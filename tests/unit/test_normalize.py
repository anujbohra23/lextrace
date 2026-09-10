"""Pure normalization and origin validation."""

import json
from pathlib import Path

import pytest

from lextrace.ingestion.courtlistener import (
    ClusterResponse,
    DocketResponse,
    IngestionError,
    OpinionResponse,
)
from lextrace.ingestion.normalize import normalize_case


def payloads() -> tuple[ClusterResponse, DocketResponse, list[OpinionResponse]]:
    data = json.loads(
        (Path(__file__).parents[1] / "fixtures/courtlistener_case.json").read_text()
    )
    return (
        ClusterResponse.model_validate(data["cluster"]),
        DocketResponse.model_validate(data["docket"]),
        [OpinionResponse.model_validate(item) for item in data["opinions"]],
    )


def test_normalization() -> None:
    case = normalize_case(*payloads())
    assert case.name == "Example v. Test"
    assert str(case.date_filed) == "2024-01-02"
    assert case.court_id == "scotus"
    assert case.docket_number == "23-1"
    assert str(case.source_url) == "https://www.courtlistener.com/opinion/1/example/"
    assert case.opinions[0].text == "First & second.\n\nNext paragraph."
    assert case.opinions[0].text_source_field == "html_with_citations"
    assert case.opinions[0].kind == "010combined"
    assert case.opinions[1].text == "Separate opinion."
    assert case.opinions[1].kind == "unknown"
    assert case.opinions[1].text_source_field == "plain_text"


@pytest.mark.parametrize("html", [None, "", "<p> </p>", "<script>hidden</script>"])
def test_plain_text_fallback(html: str | None) -> None:
    cluster, docket, opinions = payloads()
    opinions[0].html_with_citations = html
    case = normalize_case(cluster, docket, opinions)
    assert case.opinions[0].text == "not preferred"


def test_missing_text() -> None:
    cluster, docket, opinions = payloads()
    opinions[0].html_with_citations = None
    opinions[0].plain_text = " "
    with pytest.raises(IngestionError, match="text is unavailable"):
        normalize_case(cluster, docket, opinions)


@pytest.mark.parametrize(
    "path",
    [
        "https://evil.test/opinion/1/",
        "//evil.test/opinion/1/",
        "/opinion/../evil",
        "/opinion/%2e%2e/evil",
        "/opinion/1/?x=a",
        "/opinion/1/#x",
        "/opinion/1/\\evil",
        "opinion/1/",
    ],
)
def test_unsafe_public_paths(path: str) -> None:
    cluster, docket, opinions = payloads()
    cluster.absolute_url = path
    with pytest.raises(IngestionError):
        normalize_case(cluster, docket, opinions)


def test_optional_values_and_unknown_type() -> None:
    cluster, docket, opinions = payloads()
    cluster.date_filed = None
    docket.docket_number = " "
    opinions[0].type = " new-provider-type "
    assert (
        normalize_case(cluster, docket, opinions).opinions[0].kind
        == "new-provider-type"
    )
    opinions[0].type = " "
    case = normalize_case(cluster, docket, opinions)
    assert case.opinions[0].kind == "unknown"
    assert case.date_filed is None
    assert case.docket_number is None


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("<p>The court\nheld this.</p>", "The court held this."),
        ("<td>First</td><td>Second</td>", "First Second"),
        ("<p>First.</p>\n <p>Second.</p>", "First.\n\nSecond."),
        ("<p>First<br>Second<br/>Third</p>", "First\nSecond\nThird"),
        (
            "<table><tr><td>A</td><td>B</td></tr><tr><td>C</td><td>D</td></tr></table>",
            "A B\n\nC D",
        ),
        ("<p> A &amp; <b>B</b>&nbsp; C </p>", "A & B C"),
        ("<style>hidden</style><p>Visible</p><script>hidden</script>", "Visible"),
        ("\n <div><p> A   B </p><p> </p><p>C</p></div>\n", "A B\n\nC"),
    ],
)
def test_html_structure(html: str, expected: str) -> None:
    cluster, docket, opinions = payloads()
    opinions[0].html_with_citations = html
    case = normalize_case(cluster, docket, opinions)
    assert case.opinions[0].text == expected
    assert case.opinions[0].text_source_field == "html_with_citations"


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("<p>A libertyto all.</p>", "A libertyto all."),
        ("<p>§ 25 ; Ohio Rev.Code Ann.</p>", "§ 25 ; Ohio Rev.Code Ann."),
        (
            "<p>Appendix A,\n <em>\n infra\n </em>\n . Next.</p>",
            "Appendix A, infra. Next.",
        ),
        ("<p><em>inter</em>state</p>", "interstate"),
        ("<p><em>word</em> next</p>", "word next"),
        ("<p><em>word</em> ; next</p>", "word ; next"),
        ("<p><em>word</em>\n, next</p>", "word, next"),
    ],
)
def test_live_inline_spacing(html: str, expected: str) -> None:
    cluster, docket, opinions = payloads()
    opinions[0].html_with_citations = html
    assert normalize_case(cluster, docket, opinions).opinions[0].text == expected

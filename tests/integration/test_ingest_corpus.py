"""Bounded sequential ingestion with mocked HTTP and clocks."""

from datetime import date
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest

from lextrace.corpus import CorpusQuery, Manifest, manifest_path, read_cases
from lextrace.ingestion.corpus import ingest_corpus
from lextrace.ingestion.courtlistener import RequestFailure

BASE = "https://www.courtlistener.com/api/rest/v4/"
TOKEN = "synthetic-secret"


def cluster(identifier: int) -> dict[str, object]:
    return {
        "id": identifier,
        "case_name": f"Case {identifier}",
        "absolute_url": f"/opinion/{identifier}/case/",
        "date_filed": "2020-01-01",
        "docket": f"{BASE}dockets/{identifier}/",
        "sub_opinions": [f"{BASE}opinions/{identifier}/"],
        "citations": [{"volume": "1", "reporter": "F.3d", "page": "2"}],
    }


def details(request: httpx.Request) -> httpx.Response:
    identifier = int(request.url.path.rstrip("/").split("/")[-1])
    if "/dockets/" in request.url.path:
        return httpx.Response(
            200,
            json={
                "id": identifier,
                "court_id": "ca2",
                "docket_number": str(identifier),
            },
        )
    return httpx.Response(
        200,
        json={
            "id": identifier,
            "type": "010combined",
            "plain_text": "libertyto § 25 ; Rev.Code",
        },
    )


def test_pages_duplicates_rejections_and_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    query = CorpusQuery(
        court="ca2",
        max_cases=2,
        filed_after=date(2020, 1, 1),
        filed_before=date(2020, 12, 31),
    )
    calls: list[httpx.Request] = []
    pauses: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.host == "www.courtlistener.com"
        assert request.headers["Authorization"] == f"Token {TOKEN}"
        if request.url.path.endswith("clusters/"):
            assert request.url.params["date_filed__gte"] == "2020-01-01"
            if "cursor" not in request.url.params:
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            cluster(1),
                            {"id": 8, "case_name": TOKEN},
                            cluster(1),
                        ],
                        "next": BASE
                        + "clusters/?"
                        + urlencode({**query.api_filters(), "cursor": "page2"}),
                    },
                )
            return httpx.Response(
                200, json={"results": [cluster(2), cluster(3)], "next": None}
            )
        return details(request)

    output = Path("data/sample.jsonl")
    manifest = ingest_corpus(
        query,
        output,
        TOKEN,
        request_interval=15,
        transport=httpx.MockTransport(handler),
        clock=lambda: 0,
        sleep=pauses.append,
    )
    assert [case.source_id for case in read_cases(output)] == ["1", "2"]
    assert read_cases(output)[0].reporter_citations == ["1 F.3d 2"]
    assert manifest.runs[-1].quality() == {
        "source_records_encountered": 4,
        "successfully_normalized_cases": 2,
        "rejected_records": 1,
        "rejection_reason_counts": {"invalid_cluster": 1},
        "skipped_duplicate_records": 1,
    }
    assert len(calls) == 6
    assert pauses == [15] * 5
    assert TOKEN not in output.read_text() + manifest_path(output).read_text()
    before = output.read_bytes()

    def no_request(request: httpx.Request) -> httpx.Response:
        pytest.fail("Complete resume must not request data")

    ingest_corpus(
        query,
        output,
        TOKEN,
        request_interval=15,
        resume=True,
        transport=httpx.MockTransport(no_request),
    )
    assert output.read_bytes() == before


@pytest.mark.parametrize(
    "failure", ["429", "401", "403", "404", "500", "timeout", "network", "interrupt"]
)
def test_failure_and_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    monkeypatch.chdir(tmp_path)
    output = Path("data/sample.jsonl")
    query = CorpusQuery(court="ca2", max_cases=2)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("clusters/"):
            return httpx.Response(
                200, json={"results": [cluster(1), cluster(2)], "next": None}
            )
        if request.url.path.endswith("dockets/2/"):
            if failure == "timeout":
                raise httpx.ReadTimeout(TOKEN)
            if failure == "network":
                raise httpx.ConnectError(TOKEN)
            if failure == "interrupt":
                raise KeyboardInterrupt
            return httpx.Response(
                int(failure), headers={"Retry-After": "120"}, text=TOKEN
            )
        return details(request)

    with pytest.raises((RequestFailure, KeyboardInterrupt)) as error:
        ingest_corpus(
            query,
            output,
            TOKEN,
            request_interval=1,
            transport=httpx.MockTransport(handler),
            sleep=lambda _: None,
        )
    assert TOKEN not in str(error.value)
    if failure == "429":
        assert "120 seconds" in str(error.value)
    assert len(read_cases(output)) == 1
    assert calls.count("/api/rest/v4/dockets/2/") == 1
    manifest = Manifest.model_validate_json(manifest_path(output).read_bytes())
    assert manifest.runs[-1].status == (
        "interrupted" if failure == "interrupt" else "failed"
    )
    calls.clear()

    def resumed(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("clusters/"):
            return httpx.Response(
                200, json={"results": [cluster(1), cluster(2)], "next": None}
            )
        return details(request)

    manifest = ingest_corpus(
        query,
        output,
        TOKEN,
        request_interval=1,
        resume=True,
        transport=httpx.MockTransport(resumed),
        sleep=lambda _: None,
    )
    assert len(read_cases(output)) == 2
    assert "/api/rest/v4/dockets/1/" not in calls
    assert manifest.runs[-1].status == "complete"


@pytest.mark.parametrize("bad", ["text", "schema", "json", "link", "citations"])
def test_bad_case_does_not_stop_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    monkeypatch.chdir(tmp_path)
    one = cluster(1)
    if bad == "link":
        one["docket"] = "https://evil.test/dockets/1/"
    if bad == "citations":
        one["citations"] = [{"reporter": " "}]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("clusters/"):
            return httpx.Response(
                200, json={"results": [one, cluster(2)], "next": None}
            )
        if request.url.path.endswith("opinions/1/"):
            if bad == "json":
                return httpx.Response(200, text=TOKEN)
            if bad == "schema":
                return httpx.Response(
                    200, json={"id": 1, "plain_text": {"secret": TOKEN}}
                )
            if bad == "text":
                return httpx.Response(200, json={"id": 1, "plain_text": " "})
        return details(request)

    output = Path("data/sample.jsonl")
    manifest = ingest_corpus(
        CorpusQuery(court="ca2", max_cases=1),
        output,
        TOKEN,
        request_interval=1,
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )
    assert [case.source_id for case in read_cases(output)] == ["2"]
    assert len(manifest.runs[-1].rejections) == 1


def test_source_exhausted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"results": [], "next": None})
    )
    manifest = ingest_corpus(
        CorpusQuery(court="ca2", max_cases=5),
        Path("data/empty.jsonl"),
        TOKEN,
        request_interval=1,
        transport=transport,
    )
    assert manifest.runs[-1].status == "exhausted"


@pytest.mark.parametrize(
    "next_link",
    [
        "https://evil.test/?cursor=x",
        "repeat",
        "https://www.courtlistener.com/api/rest/v4/clusters/?cursor=x&docket__court=scotus&order_by=id",
    ],
)
def test_bad_pagination_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, next_link: str
) -> None:
    monkeypatch.chdir(tmp_path)
    query = CorpusQuery(court="ca2", max_cases=1)
    link = (
        BASE + "clusters/?" + urlencode({**query.api_filters(), "cursor": "x"})
        if next_link == "repeat"
        else next_link
    )
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"results": [], "next": link})

    with pytest.raises(RequestFailure):
        ingest_corpus(
            query,
            Path("data/sample.jsonl"),
            TOKEN,
            request_interval=1,
            transport=httpx.MockTransport(handler),
            sleep=lambda _: None,
        )
    assert calls <= 2


@pytest.mark.parametrize(
    "payload", [{"results": "bad", "next": None}, {"results": []}, None]
)
def test_malformed_page_is_request_failure(
    payload: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    with pytest.raises(RequestFailure, match="invalid cluster page"):
        ingest_corpus(
            CorpusQuery(court="ca2", max_cases=1),
            Path("data/a.jsonl"),
            TOKEN,
            request_interval=1,
            transport=transport,
        )
    assert read_cases(Path("data/a.jsonl")) == []


def test_all_records_rejected_exhausts_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    one = cluster(1)
    one["date_filed"] = "2019-01-01"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("clusters/"):
            return httpx.Response(200, json={"results": [None, one], "next": None})
        return details(request)

    manifest = ingest_corpus(
        CorpusQuery(court="ca2", max_cases=1, filed_after=date(2020, 1, 1)),
        Path("data/a.jsonl"),
        TOKEN,
        request_interval=1,
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )
    assert manifest.runs[-1].status == "exhausted"
    assert manifest.runs[-1].quality()["rejection_reason_counts"] == {
        "invalid_cluster": 1,
        "filter_mismatch": 1,
    }
    assert read_cases(Path("data/a.jsonl")) == []

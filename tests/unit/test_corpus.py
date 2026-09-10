"""Deterministic local corpus storage and safe resume."""

import json
from pathlib import Path

import pytest

from lextrace.corpus import (
    CorpusError,
    CorpusQuery,
    atomic_write,
    prepare,
    read_cases,
    serialize_cases,
)
from lextrace.domain.case import Case


def sample(identifier: str = "1") -> Case:
    return Case.model_validate(
        {
            "source_id": identifier,
            "source_url": "https://www.courtlistener.com/opinion/1/a/",
            "name": "A v. B",
            "date_filed": "2020-01-01",
            "court_id": "ca2",
            "docket_number": "1",
            "reporter_citations": ["1 F.3d 2"],
            "opinions": [
                {
                    "source_id": "3",
                    "kind": "unknown",
                    "text": "libertyto § 25 ; Rev.Code\nNext",
                    "text_source_field": "plain_text",
                }
            ],
        }
    )


def test_canonical_serialization(tmp_path: Path) -> None:
    first, second = sample("2"), sample("10")
    content = serialize_cases([second, first])
    assert content == serialize_cases([first, second])
    assert content.endswith("\n")
    assert len(content.splitlines()) == 2
    assert json.loads(content.splitlines()[0])["source_id"] == "2"
    path = tmp_path / "cases.jsonl"
    atomic_write(path, content)
    assert read_cases(path) == [first, second]
    assert read_cases(path)[0].opinions[0].text == first.opinions[0].text


def test_prepare_resume_and_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    output = Path("data/cases.jsonl")
    query = CorpusQuery(court="ca2", max_cases=2)
    cases, _ = prepare(output, query, False)
    assert cases == []
    atomic_write(output, serialize_cases([sample()]))
    assert len(prepare(output, query, True)[0]) == 1
    with pytest.raises(CorpusError, match="already exists"):
        prepare(output, query, False)
    with pytest.raises(CorpusError, match="must match"):
        prepare(output, CorpusQuery(court="ca2", max_cases=3), True)
    atomic_write(output, serialize_cases([sample(), sample()]))
    with pytest.raises(CorpusError, match="duplicate"):
        prepare(output, query, True)


@pytest.mark.parametrize(
    "content", ["{broken\n", "{}\n", "\n", '{"source_id":"secret"}\n']
)
def test_invalid_jsonl(content: str, tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text(content)
    with pytest.raises(CorpusError, match="line 1") as error:
        read_cases(path)
    assert "secret" not in str(error.value)


def test_atomic_failure_preserves_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text("original")

    def fail(source: str, target: Path) -> None:
        raise OSError("sensitive detail")

    monkeypatch.setattr("lextrace.corpus.os.replace", fail)
    with pytest.raises(CorpusError, match="Could not write"):
        atomic_write(path, "replacement")
    assert path.read_text() == "original"
    assert list(tmp_path.iterdir()) == [path]


def test_output_must_be_under_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(CorpusError, match="under data"):
        prepare(Path("elsewhere.jsonl"), CorpusQuery(court="ca2", max_cases=1), False)


def test_resume_recovers_count_between_file_replacements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lextrace.corpus import IngestionRun, save_manifest

    monkeypatch.chdir(tmp_path)
    output = Path("data/sample.jsonl")
    query = CorpusQuery(court="ca2", max_cases=2)
    _, manifest = prepare(output, query, False)
    manifest.runs.append(IngestionRun(request_interval=15, initial_case_count=0))
    save_manifest(output, manifest)
    # Simulate interruption after JSONL replacement but before metadata replacement.
    atomic_write(output, serialize_cases([sample()]))
    _, recovered = prepare(output, query, True)
    assert recovered.runs[-1].status == "interrupted"
    assert recovered.runs[-1].successfully_normalized_cases == 1

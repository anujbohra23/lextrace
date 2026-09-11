"""CLI operation over a temporary corpus without models or network."""

import json
from pathlib import Path

import pytest

from lextrace.cli import main
from lextrace.corpus import serialize_cases
from lextrace.domain.case import Case
from lextrace.evaluation.benchmark import BenchmarkConfig, BenchmarkInputs

Sample = tuple[list[Case], BenchmarkInputs, BenchmarkConfig]


def test_build_info_search(
    benchmark_sample: Sample,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("COURTLISTENER_API_TOKEN", raising=False)
    cases, _, _ = benchmark_sample
    source = Path("cases.jsonl")
    source.write_text(serialize_cases(cases[:3]))
    index = "artifacts/indexes/default"
    main(["build-index", "--corpus", str(source), "--output", index, "--lexical-only"])
    result = capsys.readouterr()
    assert not result.err
    assert json.loads(result.out)["document_count"] == 3
    main(["index-info", index])
    assert json.loads(capsys.readouterr().out)["dense"] is False
    main(
        [
            "search",
            "synthetic",
            "--mode",
            "bm25",
            "--top-k",
            "2",
            "--court",
            "ca2",
            "--json",
        ]
    )
    result = capsys.readouterr()
    assert not result.err
    data = json.loads(result.out)
    assert len(data["results"]) == 2
    assert all(r["court"] == "ca2" for r in data["results"])
    assert data["results"][0]["relevant_passage"]["text"]
    main(["search", "synthetic", "--mode", "bm25", "--top-k", "1"])
    text = capsys.readouterr().out
    assert "Score:" in text and "https://www.courtlistener.com/opinion/" in text
    with pytest.raises(SystemExit) as error:
        main(["search", "x", "--mode", "reranked"])
    assert error.value.code == 1
    result = capsys.readouterr()
    assert not result.out and "dense index" in result.err


@pytest.mark.parametrize(
    "args",
    [
        ["--top-k", "0"],
        ["--filed-after", "2020-01-01", "--filed-before", "2000-01-01"],
        ["--mode", "not-a-mode"],
    ],
)
def test_usage_errors(args: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["search", "x", *args])
    assert error.value.code == 2
    assert not capsys.readouterr().out

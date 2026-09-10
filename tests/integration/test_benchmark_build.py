"""Offline benchmark publication, CLI validation, and tamper detection."""

import json
from pathlib import Path

import pytest

from lextrace.cli import main
from lextrace.corpus import serialize_cases
from lextrace.domain.case import Case
from lextrace.evaluation.benchmark import (
    BenchmarkConfig,
    BenchmarkError,
    BenchmarkInputs,
    canonical,
)
from lextrace.evaluation.benchmark_build import build_benchmark, validate_benchmark

Sample = tuple[list[Case], BenchmarkInputs, BenchmarkConfig]


def test_offline_cli_roundtrip(
    benchmark_sample: Sample,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cases, inputs, config = benchmark_sample
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("COURTLISTENER_API_TOKEN", raising=False)

    # A guard proves that neither CLI command creates an HTTP client.
    def forbid_network(*args: object, **kwargs: object) -> None:
        pytest.fail("Benchmark construction must be offline")

    monkeypatch.setattr("httpx.Client", forbid_network)
    Path("sources.jsonl").write_text(serialize_cases(cases))
    Path("inputs.json").write_text(canonical(inputs))
    Path("config.json").write_text(canonical(config))
    output = "data/benchmarks/v1/frozen"
    main(
        [
            "build-benchmark",
            "--corpus",
            "sources.jsonl",
            "--inputs",
            "inputs.json",
            "--config",
            "config.json",
            "--output",
            output,
        ]
    )
    response = capsys.readouterr()
    assert response.err == ""
    assert json.loads(response.out) == {
        "status": "built",
        "queries": 25,
        "candidate_cases": 300,
    }
    main(["validate-benchmark", output])
    assert json.loads(capsys.readouterr().out)["status"] == "valid"
    assert {p.name for p in Path(output).iterdir()} == {
        "manifest.json",
        "config.json",
        "sources.jsonl",
        "candidates.jsonl",
        "queries.jsonl",
        "provenance.json",
    }
    with pytest.raises(BenchmarkError, match="exists"):
        build_benchmark(
            Path("sources.jsonl"),
            Path("inputs.json"),
            Path("config.json"),
            Path(output),
        )
    path = Path(output) / "queries.jsonl"
    path.write_text(path.read_text().replace('"split":"dev"', '"split":"test"', 1))
    with pytest.raises(BenchmarkError, match="do not match"):
        validate_benchmark(Path(output))


@pytest.mark.parametrize(
    "artifact",
    ["candidates.jsonl", "sources.jsonl", "provenance.json", "manifest.json"],
)
def test_corrupt_bundle(
    artifact: str,
    benchmark_sample: Sample,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases, inputs, config = benchmark_sample
    monkeypatch.chdir(tmp_path)
    Path("sources.jsonl").write_text(serialize_cases(cases))
    Path("inputs.json").write_text(canonical(inputs))
    Path("config.json").write_text(canonical(config))
    output = Path("data/benchmarks/v1/frozen")
    build_benchmark(
        Path("sources.jsonl"), Path("inputs.json"), Path("config.json"), output
    )
    (output / artifact).write_text("private invalid data")
    with pytest.raises(BenchmarkError) as error:
        validate_benchmark(output)
    assert "private" not in str(error.value)


def test_invalid_cli_input_is_safe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as error:
        main(["validate-benchmark", str(tmp_path)])
    assert error.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("Error:")

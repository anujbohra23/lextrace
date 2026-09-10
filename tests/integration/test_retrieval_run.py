"""Complete offline candidate bundle → BM25 → metrics; fixture is synthetic."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from lextrace.cli import main
from lextrace.domain.case import Case
from lextrace.evaluation.benchmark import (
    BenchmarkConfig,
    BenchmarkError,
    BenchmarkInputs,
    QuerySelection,
)
from lextrace.evaluation.benchmark_build import assemble, validate_benchmark
from lextrace.evaluation.excerpts import propose_excerpt

Sample = tuple[list[Case], BenchmarkInputs, BenchmarkConfig]


def provisional(inputs: BenchmarkInputs) -> BenchmarkInputs:
    return inputs.model_copy(
        update={
            "selections": [
                QuerySelection.model_validate(
                    {
                        **s.model_dump(),
                        "review_status": "review_required",
                        "leakage_reviewer": None,
                        "leakage_review_approved": False,
                        "known_alias_review_complete": False,
                        "audit_reviewer": None,
                        "positive_audits": [],
                    }
                )
                for s in inputs.selections
            ]
        }
    )


def test_provisional_baseline(
    benchmark_sample: Sample,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("COURTLISTENER_API_TOKEN", raising=False)

    def no_network(*args: object, **kwargs: object) -> None:
        pytest.fail("Offline only")

    monkeypatch.setattr("httpx.Client", no_network)
    cases, inputs, config = benchmark_sample
    inputs = provisional(inputs)
    artifacts = assemble(cases, inputs, config)
    assert artifacts == assemble(list(reversed(cases)), inputs, config)
    manifest = json.loads(artifacts["manifest.json"])
    assert manifest["review_status"] == "review_required"
    assert (
        manifest["leakage_reviewed_queries"] == 0
        and manifest["audited_query_ids"] == []
    )
    bundle = Path("data/benchmarks/v1/candidate")
    bundle.mkdir(parents=True)
    for name, content in artifacts.items():
        (bundle / name).write_text(content)
    main(["run-bm25", str(bundle), "--output", "artifacts/run1"])
    captured = capsys.readouterr()
    assert not captured.err
    summary = json.loads(captured.out)
    assert summary["status"] == "PROVISIONAL" and summary["candidate_count"] == 300
    assert set(summary["metrics"]) == {"dev", "test", "overall"}
    ranks = Path("artifacts/run1/rankings.jsonl").read_text()
    assert len(ranks.splitlines()) == 25 * 300
    main(["run-bm25", str(bundle), "--output", "artifacts/run2"])
    assert Path("artifacts/run2/rankings.jsonl").read_text() == ranks
    (bundle / "audit.json").write_text("tampered")
    with pytest.raises(BenchmarkError):
        validate_benchmark(bundle)


def test_honest_review(benchmark_sample: Sample) -> None:
    _, inputs, _ = benchmark_sample
    raw = inputs.selections[0].model_dump()
    with pytest.raises(ValidationError):
        QuerySelection.model_validate({**raw, "review_status": "review_required"})
    with pytest.raises(ValidationError):
        QuerySelection.model_validate({**raw, "leakage_review_approved": False})


def test_excerpt_offsets_and_review_status(benchmark_sample: Sample) -> None:
    cases, _, _ = benchmark_sample
    case = cases[-1]
    op = case.opinions[0]
    # Introductory prose followed by a citation in a separate paragraph.
    facts = " ".join(
        ["The claimant challenges the administrative procedure and seeks relief."] * 25
    )
    op = op.model_copy(update={"text": facts + "\n\nExample v. Respondent, 1 F.3d 20."})
    selection = propose_excerpt(case, op, ["Example v. Respondent", "1 F.3d 20"])
    assert selection.review_status == "review_required"
    assert selection.leakage_reviewer is None
    assert selection.excerpt.start == 0
    assert op.text.startswith(facts)


def test_excerpt_fails_closed(benchmark_sample: Sample) -> None:
    cases, _, _ = benchmark_sample
    case = cases[-1]
    op = case.opinions[0].model_copy(update={"text": "Cited v. Case, 1 F.3d 1. " * 500})
    with pytest.raises(BenchmarkError):
        propose_excerpt(case, op, ["Cited v. Case"])

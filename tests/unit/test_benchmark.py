"""Benchmark invariants, query fidelity, and frozen weak-label construction."""

from datetime import date

import pytest
from pydantic import ValidationError

from lextrace.domain.case import Case
from lextrace.evaluation.benchmark import (
    BenchmarkConfig,
    BenchmarkError,
    BenchmarkInputs,
    BenchmarkItem,
    BenchmarkManifest,
    Removal,
    Span,
    digest,
)
from lextrace.evaluation.benchmark_build import assemble

Sample = tuple[list[Case], BenchmarkInputs, BenchmarkConfig]


def test_valid_benchmark_and_determinism(benchmark_sample: Sample) -> None:
    cases, inputs, config = benchmark_sample
    before = [case.model_dump_json() for case in cases]
    artifact = assemble(cases, inputs, config)
    items = [
        BenchmarkItem.model_validate_json(line)
        for line in artifact["queries.jsonl"].splitlines()
    ]
    manifest = BenchmarkManifest.model_validate_json(artifact["manifest.json"])
    assert len(items) == 25
    assert sum(item.split == "dev" for item in items) == 10
    assert sum(item.split == "test" for item in items) == 15
    assert len(manifest.candidate_case_ids) == 300
    assert set(manifest.positive_union_ids) == {"1", "2"}
    assert all(item.positive_case_ids == ["1", "2"] for item in items)
    assert manifest.candidate_strata
    assert [case.model_dump_json() for case in cases] == before
    inputs.selections.reverse()
    inputs.citations.reverse()
    inputs.opinion_mappings.reverse()
    assert assemble(list(reversed(cases)), inputs, config) == artifact


def test_temporal_cutoff_is_fixed() -> None:
    with pytest.raises(ValidationError):
        BenchmarkConfig(candidate_cutoff=date(2010, 1, 1))


@pytest.mark.parametrize(
    "bad_date", [None, date(2010, 1, 1), date(2010, 1, 2), date(2011, 1, 1)]
)
def test_temporal_positive_exclusion(
    bad_date: date | None, benchmark_sample: Sample
) -> None:
    cases, inputs, config = benchmark_sample
    inputs.opinion_mappings[0].date_filed = bad_date
    cases[0].date_filed = bad_date
    with pytest.raises(BenchmarkError, match="fewer than two"):
        assemble(cases, inputs, config)


def test_missing_positive(benchmark_sample: Sample) -> None:
    cases, inputs, config = benchmark_sample
    with pytest.raises(BenchmarkError, match="positive Case is missing"):
        assemble(cases[1:], inputs, config)


def test_duplicate_candidate(benchmark_sample: Sample) -> None:
    cases, inputs, config = benchmark_sample
    with pytest.raises(BenchmarkError, match="Duplicate source case"):
        assemble([*cases, cases[0]], inputs, config)


@pytest.mark.parametrize("reference", ["mapping", "opinion", "case", "hash"])
def test_invalid_references(reference: str, benchmark_sample: Sample) -> None:
    cases, inputs, config = benchmark_sample
    if reference == "mapping":
        inputs.opinion_mappings.pop()
    elif reference == "opinion":
        inputs.selections[0].source_opinion_id = "999999"
    elif reference == "case":
        inputs.selections[0].source_case_id = "999999"
    else:
        inputs.selections[0].source_text_sha256 = "0" * 64
    with pytest.raises(BenchmarkError):
        assemble(cases, inputs, config)


@pytest.mark.parametrize(
    "leak",
    [
        "1 F.3d 11",
        "Candidate 1",
        "Candidate 1 v. Respondent",
        "Id. at 10",
        "supra",
        "999 U.S. 123",
    ],
)
def test_query_leakage(leak: str, benchmark_sample: Sample) -> None:
    cases, inputs, config = benchmark_sample
    selection = inputs.selections[0]
    opinion = cases[320].opinions[0]
    opinion.text = leak + " " + opinion.text
    selection.source_text_sha256 = digest(opinion.text)
    selection.excerpt.end += len(leak) + 1
    with pytest.raises(BenchmarkError, match="Query contains"):
        assemble(cases, inputs, config)


def test_removal_preserves_source_and_offsets(benchmark_sample: Sample) -> None:
    cases, inputs, config = benchmark_sample
    selection = inputs.selections[0]
    opinion = cases[320].opinions[0]
    prefix = "Candidate 1 v. Respondent, 1 F.3d 11. "
    opinion.text = prefix + opinion.text
    original = opinion.text
    selection.source_text_sha256 = digest(original)
    selection.excerpt.end += len(prefix)
    selection.removals = [
        Removal(start=0, end=len(prefix), reason="citation_bearing_clause")
    ]
    artifacts = assemble(cases, inputs, config)
    item = BenchmarkItem.model_validate_json(artifacts["queries.jsonl"].splitlines()[0])
    assert not item.query_text.startswith("Candidate")
    assert opinion.text == original
    selection.removals.append(
        Removal(start=1, end=len(prefix), reason="case_reference")
    )
    with pytest.raises(BenchmarkError, match="overlap"):
        assemble(cases, inputs, config)


def test_litigation_split_conflict(benchmark_sample: Sample) -> None:
    cases, inputs, config = benchmark_sample
    inputs.litigation_groups = {"related": ["1010", "1011"]}
    with pytest.raises(BenchmarkError, match="crosses"):
        assemble(cases, inputs, config)


def test_missing_audit(benchmark_sample: Sample) -> None:
    cases, inputs, config = benchmark_sample
    inputs.selections[0].positive_audits.pop()
    with pytest.raises(BenchmarkError, match="every eligible positive"):
        assemble(cases, inputs, config)


def test_changed_seed_changes_only_distractors(benchmark_sample: Sample) -> None:
    cases, inputs, config = benchmark_sample
    first = assemble(cases, inputs, config)
    config.selection_seed += 1
    second = assemble(cases, inputs, config)
    assert first["queries.jsonl"] == second["queries.jsonl"]
    assert first["candidates.jsonl"] != second["candidates.jsonl"]


def test_bad_span() -> None:
    with pytest.raises(ValidationError):
        Span(start=5, end=4)


def test_positive_union_can_exceed_target(benchmark_sample: Sample) -> None:
    from lextrace.evaluation.benchmark import OpinionMapping

    cases, inputs, config = benchmark_sample
    for case in cases[2:305]:
        inputs.opinion_mappings.append(
            OpinionMapping(
                opinion_id=case.opinions[0].source_id,
                case_id=case.source_id,
                date_filed=case.date_filed,
                court=case.court_id,
                case_name=case.name,
                reporter_citations=case.reporter_citations or [],
                payload_sha256=digest("synthetic mapping " + case.source_id),
            )
        )
    inputs.citations[0].cited_opinion_ids = [
        case.opinions[0].source_id for case in cases[:305]
    ]
    audit = inputs.selections[0].positive_audits[0]
    inputs.selections[0].positive_audits = [
        audit.model_copy(update={"case_id": case.source_id}) for case in cases[:305]
    ]
    manifest = BenchmarkManifest.model_validate_json(
        assemble(cases, inputs, config)["manifest.json"]
    )
    assert len(manifest.positive_union_ids) == 305
    assert len(manifest.candidate_case_ids) == 305


@pytest.mark.parametrize(
    "issue", ["year", "court", "short", "audit_count", "small_pool"]
)
def test_v1_scope_rejections(issue: str, benchmark_sample: Sample) -> None:
    cases, inputs, config = benchmark_sample
    if issue == "year":
        cases[320].date_filed = date(2011, 1, 1)
    elif issue == "court":
        cases[320].court_id = "ca9"
    elif issue == "short":
        inputs.selections[0].excerpt.end = 40
    elif issue == "audit_count":
        inputs.selections[0].audit_reviewer = None
        inputs.selections[0].positive_audits = []
    else:
        cases = cases[:190] + cases[320:]
    with pytest.raises(BenchmarkError):
        assemble(cases, inputs, config)

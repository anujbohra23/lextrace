"""Independent verification uses synthetic source text and no providers."""

from dataclasses import replace
from datetime import date

import pytest

from lextrace.investigations.contracts import (
    ExtractionSpan,
    InterpretationBasis,
    InvestigationEvent,
    ReviewState,
    TemporalReference,
)
from lextrace.investigations.extraction_verification import verify_extraction
from lextrace.investigations.factual_extraction import (
    ExtractionResult,
    extract_factual_candidates,
)
from tests.unit.test_investigation_factual_extraction import FakeLLM, candidate, source


def extraction(text: str = "😀 Alice / Alice next day") -> ExtractionResult:
    version, sections = source(text)
    return extract_factual_candidates(
        version,
        text,
        sections,
        FakeLLM(
            [
                {
                    "candidates": [
                        candidate("Alice", 2),
                        candidate("Alice", 10),
                        candidate(text, kind="assertion"),
                        candidate(text, kind="event"),
                        candidate("next day", text.index("next day"), kind="temporal"),
                    ]
                }
            ]
        ),
        run_id="run",
    )


def test_valid_independent_deterministic_and_never_reviewed() -> None:
    text = "😀 Alice / Alice next day"
    version, sections = source(text)
    result = extraction(text)
    verified = verify_extraction(result, version, sections, text, run_id="run")
    assert verified == verify_extraction(result, version, sections, text, run_id="run")
    assert len(verified.valid_candidates) == 5
    assert not verified.quarantined_candidates
    assert verified.coverage.status == "COMPLETE"
    assert verified.coverage.original.processed_pages == ()
    for record in verified.valid_candidates[2:]:
        assert record.review_state == "PROPOSED"  # type: ignore[union-attr]
        assert record.interpretation_basis == "SOURCE_ASSERTION"  # type: ignore[union-attr]
        assert record.verification_state == "VALID"  # type: ignore[union-attr]
    assert all(d.requires_human_review for d in verified.diagnostics[2:])
    temporal = verified.valid_candidates[-1]
    assert isinstance(temporal, TemporalReference)
    assert temporal.earliest is None and temporal.latest is None
    assert result.candidates[2].verification_state == "UNVERIFIED"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "field, value, code",
    [
        ("matter_id", "wrong", "MATTER_MISMATCH"),
        ("document_id", "wrong", "DOCUMENT_MISMATCH"),
        ("version_id", "wrong", "VERSION_MISMATCH"),
        ("run_id", "wrong", "RUN_MISMATCH"),
    ],
)
def test_wrong_record_ownership(field: str, value: str, code: str) -> None:
    text = "😀 Alice / Alice next day"
    version, sections = source(text)
    result = extraction(text)
    bad = result.candidates[0].model_copy(update={field: value})
    verified = verify_extraction(
        replace(result, candidates=(bad, *result.candidates[1:])),
        version,
        sections,
        text,
        run_id="run",
    )
    assert len(verified.valid_candidates) == 4
    assert code in verified.quarantined_candidates[0].issue_codes


@pytest.mark.parametrize(
    "changes, code",
    [
        ({"exact_quote": "Ghost"}, "QUOTE_MISMATCH"),
        ({"start": 3, "end": 8}, "QUOTE_MISMATCH"),
        ({"start": -1}, "MALFORMED_REFERENCE"),
        ({"end": 999}, "SPAN_BOUNDS_INVALID"),
        ({"section_id": "invented"}, "SECTION_MISMATCH"),
        ({"parser_version": "wrong"}, "EXTRACTION_IDENTITY_MISMATCH"),
        ({"version_id": "wrong"}, "VERSION_MISMATCH"),
    ],
)
def test_bad_references_ignore_cached_valid(
    changes: dict[str, object], code: str
) -> None:
    text = "😀 Alice / Alice next day"
    version, sections = source(text)
    result = extraction(text)
    record = result.candidates[2]
    bad = record.model_copy(
        update={
            "evidence_references": (
                record.evidence_references[0].model_copy(update=changes),
            ),
            "verification_state": "VALID",
        }
    )
    verified = verify_extraction(
        replace(result, candidates=(bad, result.candidates[0])),
        version,
        sections,
        text,
        run_id="run",
    )
    assert len(verified.valid_candidates) == 1
    assert code in verified.quarantined_candidates[0].issue_codes
    assert verified.quarantined_candidates[0].candidate.verification_state == "INVALID"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "change, code",
    [
        ({"raw_expression": "tomorrow"}, "TEMPORAL_EXPRESSION_UNSOURCED"),
        (
            {
                "kind": "exact",
                "earliest": date(2020, 1, 1),
                "latest": date(2020, 1, 1),
                "precision": "day",
            },
            "TEMPORAL_BOUNDS_UNSUPPORTED",
        ),
        ({"run_id": "wrong"}, "RUN_MISMATCH"),
    ],
)
def test_nested_temporal_quarantines_parent(
    change: dict[str, object], code: str
) -> None:
    text = "😀 Alice / Alice next day"
    version, sections = source(text)
    result = extraction(text)
    event = result.candidates[3]
    assert isinstance(event, InvestigationEvent)
    time = result.candidates[-1]
    assert isinstance(time, TemporalReference)
    bad = event.model_copy(update={"temporal": time.model_copy(update=change)})
    verified = verify_extraction(
        replace(result, candidates=(bad,)), version, sections, text, run_id="run"
    )
    assert not verified.valid_candidates
    assert code in verified.quarantined_candidates[0].issue_codes


@pytest.mark.parametrize("source_status", ["PARTIAL", "OCR_REQUIRED"])
def test_source_manifest_cannot_be_upgraded(source_status: str) -> None:
    text = "😀 Alice / Alice next day"
    version, sections = source(text)
    verified = verify_extraction(
        extraction(text),
        version.model_copy(update={"coverage": source_status}),
        sections,
        text,
        run_id="run",
    )
    assert verified.coverage.status == source_status
    assert "MISLEADING_COMPLETE" in verified.coverage.issue_codes
    if source_status == "OCR_REQUIRED":
        assert not verified.valid_candidates


@pytest.mark.parametrize(
    "windows, code",
    [
        ((ExtractionSpan(start=0, end=5),), "UNCOVERED_TEXT"),
        (
            (ExtractionSpan(start=0, end=15), ExtractionSpan(start=10, end=24)),
            "COVERAGE_OVERLAP",
        ),
        ((ExtractionSpan(start=0, end=99),), "COVERAGE_BOUNDS_INVALID"),
    ],
)
def test_misleading_complete(windows: tuple[ExtractionSpan, ...], code: str) -> None:
    text = "😀 Alice / Alice next day"
    version, sections = source(text)
    result = extraction(text)
    result = replace(
        result,
        coverage=result.coverage.model_copy(update={"processed_windows": windows}),
    )
    verified = verify_extraction(result, version, sections, text, run_id="run")
    assert verified.coverage.status == "PARTIAL"
    assert code in verified.coverage.issue_codes
    assert "MISLEADING_COMPLETE" in verified.coverage.issue_codes


def test_failed_window_and_upstream_quarantine_preserved() -> None:
    text = "AliceAlice"
    version, sections = source(text)
    result = extract_factual_candidates(
        version,
        text,
        sections,
        FakeLLM([{"candidates": [candidate("Ghost")]}, RuntimeError("private")]),
        run_id="run",
        max_characters=5,
    )
    verified = verify_extraction(result, version, sections, text, run_id="run")
    assert verified.extraction_quarantine == result.quarantined
    assert verified.coverage.status == "PARTIAL"
    assert verified.coverage.uncovered_spans == (ExtractionSpan(start=5, end=10),)
    assert verified.coverage.original.skipped_spans[0].reason == (
        "MODEL_FAILURE_OR_MALFORMED_OUTPUT"
    )
    assert len(verified.coverage.original.processed_windows) == 1


def test_source_text_is_rechecked_even_without_candidates() -> None:
    text = "😀 Alice / Alice next day"
    version, sections = source(text)
    with pytest.raises(ValueError, match="trusted version"):
        verify_extraction(
            replace(extraction(text), candidates=()),
            version,
            sections,
            text.upper(),
            run_id="run",
        )


def test_invalid_nested_reference_is_independently_rechecked() -> None:
    text = "😀 Alice / Alice next day"
    version, sections = source(text)
    result = extraction(text)
    event = result.candidates[3]
    assert isinstance(event, InvestigationEvent)
    ref = event.temporal.evidence_references[0].model_copy(
        update={"exact_quote": "fabricated"}
    )
    event = event.model_copy(
        update={
            "temporal": event.temporal.model_copy(
                update={"evidence_references": (ref,), "verification_state": "VALID"}
            )
        }
    )
    verified = verify_extraction(
        replace(result, candidates=(event,)), version, sections, text, run_id="run"
    )
    assert "QUOTE_MISMATCH" in verified.quarantined_candidates[0].issue_codes


def test_forged_review_does_not_enter_valid_candidates() -> None:
    text = "😀 Alice / Alice next day"
    version, sections = source(text)
    result = extraction(text)
    record = result.candidates[2].model_copy(
        update={
            "review_state": ReviewState.ACCEPTED,
            "review_decision_id": "untrusted",
            "interpretation_basis": InterpretationBasis.REVIEWED_FINDING,
        }
    )
    verified = verify_extraction(
        replace(result, candidates=(record,)), version, sections, text, run_id="run"
    )
    assert not verified.valid_candidates
    assert (
        "UNEXPECTED_REVIEW_METADATA" in verified.quarantined_candidates[0].issue_codes
    )


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2020-01-01", True),
        ("next day", False),
        ("May 2020", False),
    ],
)
def test_only_explicit_iso_day_supports_exact_bounds(raw: str, expected: bool) -> None:
    version, sections = source(raw)
    result = extract_factual_candidates(
        version,
        raw,
        sections,
        FakeLLM(
            [
                {
                    "candidates": [
                        candidate(raw, kind="temporal", temporal_kind="approximate")
                    ]
                }
            ]
        ),
        run_id="run",
    )
    record = result.candidates[0].model_copy(
        update={
            "kind": "exact",
            "earliest": date(2020, 1, 1),
            "latest": date(2020, 1, 1),
            "precision": "day",
        }
    )
    verified = verify_extraction(
        replace(result, candidates=(record,)), version, sections, raw, run_id="run"
    )
    assert bool(verified.valid_candidates) == expected
    assert verified.diagnostics[0].requires_human_review


@pytest.mark.parametrize(
    "changes, code",
    [
        ({"text_length": 99}, "TEXT_LENGTH_MISMATCH"),
        ({"run_id": "other"}, "RUN_MISMATCH"),
        ({"processed_pages": (1,)}, "PAGE_MISMATCH"),
    ],
)
def test_coverage_identity_length_and_unknown_pages(
    changes: dict[str, object], code: str
) -> None:
    text = "😀 Alice / Alice next day"
    version, sections = source(text)
    result = extraction(text)
    verified = verify_extraction(
        replace(result, coverage=result.coverage.model_copy(update=changes)),
        version,
        sections,
        text,
        run_id="run",
    )
    assert verified.coverage.status == "PARTIAL"
    assert code in verified.coverage.issue_codes

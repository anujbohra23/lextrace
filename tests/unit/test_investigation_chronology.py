"""Synthetic trusted-result fixtures; no storage, providers or calendar inference."""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime
from itertools import permutations
from typing import Literal

import pytest

from lextrace.investigations.chronology import build_chronology
from lextrace.investigations.contracts import (
    EvidenceReference,
    ExtractionCoverage,
    InvestigationEvent,
    ReviewState,
    TemporalReference,
)
from lextrace.investigations.extraction_verification import (
    CoverageVerification,
    ExtractionVerificationResult,
    VerificationQuarantine,
)


def event(
    event_id: str,
    earliest: date | None = None,
    latest: date | None = None,
    *,
    kind: Literal["exact", "approximate", "relative", "interval", "unknown"] = "exact",
    precision: Literal["day", "month", "year", "unknown"] = "day",
    matter_id: str = "matter",
    document_id: str = "document",
) -> InvestigationEvent:
    ref = EvidenceReference(
        matter_id=matter_id,
        document_id=document_id,
        version_id="version",
        original_content_hash="a" * 64,
        extracted_text_hash="b" * 64,
        parser="synthetic",
        parser_version="1",
        extraction_settings_hash="c" * 64,
        section_id="section",
        start=0,
        end=7,
        exact_quote="account",
    )
    temporal = TemporalReference(
        matter_id=matter_id,
        document_id=document_id,
        version_id="version",
        run_id="run",
        created_at=datetime(2020, 1, 1, tzinfo=UTC),
        generator="synthetic",
        evidence_references=(ref,),
        temporal_id="time-" + event_id,
        kind=kind,
        raw_expression="source date expression",
        earliest=earliest,
        latest=latest if latest is not None else earliest,
        precision=precision,
        verification_state="VALID",
    )
    return InvestigationEvent(
        matter_id=matter_id,
        document_id=document_id,
        version_id="version",
        run_id="run",
        created_at=datetime(2020, 1, 1, tzinfo=UTC),
        generator="synthetic",
        evidence_references=(ref,),
        event_id=event_id,
        description="An account of the same occurrence",
        temporal=temporal,
        verification_state="VALID",
    )


def result(
    *events: InvestigationEvent,
    status: Literal["COMPLETE", "PARTIAL", "OCR_REQUIRED"] = "COMPLETE",
    matter_id: str = "matter",
    document_id: str = "document",
) -> ExtractionVerificationResult:
    coverage = ExtractionCoverage(
        matter_id=matter_id,
        document_id=document_id,
        version_id="version",
        run_id="run",
        created_at=datetime(2020, 1, 1, tzinfo=UTC),
        generator="synthetic",
        coverage_id="coverage",
        text_length=0,
        status=status,
        warnings=("Synthetic warning",),
        incomplete_reasons=() if status == "COMPLETE" else ("Missing source text",),
    )
    return ExtractionVerificationResult(
        events, (), (), CoverageVerification(status, coverage, (), ()), ()
    )


def test_exact_dates_ties_accounts_and_permutation_invariance() -> None:
    events = (
        event("z", date(2020, 2, 1)),
        event("b", date(2020, 1, 1)),
        event("a", date(2020, 1, 1)),
        event("early", date(2019, 1, 1)),
    )
    expected = build_chronology((result(*events),), matter_id="matter")
    assert [[e.event_id for e in g.events] for g in expected.dated_groups] == [
        ["early"],
        ["a", "b"],
        ["z"],
    ]
    for ordering in permutations(events):
        assert build_chronology((result(*ordering),), matter_id="matter") == expected
    split = (result(*events[:2]), result(*events[2:]))
    assert build_chronology(split, matter_id="matter") == expected
    assert build_chronology(tuple(reversed(split)), matter_id="matter") == expected


def test_unresolved_and_unbounded_approximate_dates() -> None:
    events = (
        event("unknown", kind="unknown", precision="unknown"),
        event("relative", kind="relative", precision="unknown"),
        event("approx", kind="approximate", precision="unknown"),
    )
    timeline = build_chronology((result(*events),), matter_id="matter")
    assert not timeline.dated_groups
    assert [e.event_id for e in timeline.unresolved_events] == [
        "approx",
        "relative",
        "unknown",
    ]
    assert all(e.temporal.earliest is None for e in timeline.unresolved_events)


def test_ranges_precision_and_transitive_overlap_remain_explicit() -> None:
    month = event(
        "month",
        date(2020, 1, 1),
        date(2020, 1, 31),
        kind="approximate",
        precision="month",
    )
    interval = event("interval", date(2020, 1, 31), date(2020, 2, 2), kind="interval")
    day = event("day", date(2020, 2, 2))
    year = event(
        "year",
        date(2021, 1, 1),
        date(2021, 12, 31),
        kind="approximate",
        precision="year",
    )
    timeline = build_chronology(
        (result(year, day, interval, month),), matter_id="matter"
    )
    first, second = timeline.dated_groups
    assert first.events == (month, interval, day)
    assert (first.earliest, first.latest) == (date(2020, 1, 1), date(2020, 2, 2))
    assert second.events == (year,)
    assert first.events[0].temporal.precision == "month"
    assert second.events[0].temporal.precision == "year"


def test_only_valid_events_are_included() -> None:
    kept = event("kept", date(2020, 1, 1))
    excluded = event("excluded", date(2019, 1, 1))
    verified = replace(
        result(kept),
        valid_candidates=(kept.temporal, kept),
        quarantined_candidates=(VerificationQuarantine(0, excluded, ("INVALID",)),),
    )
    timeline = build_chronology((verified,), matter_id="matter")
    assert timeline.dated_groups[0].events == (kept,)


def test_cross_matter_isolation_and_deterministic_diagnostics() -> None:
    wrong = event("wrong", date(2020, 1, 1), matter_id="other")
    inputs = (result(wrong), result(matter_id="other"))
    timeline = build_chronology(inputs, matter_id="matter")
    assert not timeline.dated_groups
    assert len(timeline.coverage) == 1
    assert [d.code for d in timeline.diagnostics] == [
        "COVERAGE_MATTER_MISMATCH",
        "EVENT_MATTER_MISMATCH",
    ]
    assert timeline == build_chronology(tuple(reversed(inputs)), matter_id="matter")


def test_duplicates_idempotent_conflicts_fail_and_source_scope_is_distinct() -> None:
    original = event("same", date(2020, 1, 1))
    assert build_chronology((result(original, original),), matter_id="matter") == (
        build_chronology((result(original),), matter_id="matter")
    )
    conflict = original.model_copy(update={"description": "Conflicting account"})
    for pair in ((original, conflict), (conflict, original)):
        with pytest.raises(ValueError, match="Conflicting duplicate event identity"):
            build_chronology((result(*pair),), matter_id="matter")
    other_source = event("same", date(2020, 1, 1), document_id="other")
    revision = original.model_copy(update={"revision": 2})
    timeline = build_chronology(
        (
            result(original, revision),
            result(other_source, document_id="other"),
        ),
        matter_id="matter",
    )
    assert len(timeline.dated_groups[0].events) == 3


def test_review_provenance_references_preserved_and_output_immutable() -> None:
    original = event("account", date(2020, 1, 1)).model_copy(
        update={"review_state": ReviewState.DEFERRED, "review_decision_id": "decision"}
    )
    timeline = build_chronology((result(original),), matter_id="matter")
    assert timeline.dated_groups[0].events == (original,)
    assert timeline.dated_groups[0].events[0].model_dump() == original.model_dump()
    assert original.temporal.review_state == ReviewState.PROPOSED
    with pytest.raises(FrozenInstanceError):
        timeline.matter_id = "changed"  # type: ignore[misc]


@pytest.mark.parametrize("status", ["PARTIAL", "OCR_REQUIRED"])
def test_empty_incomplete_extraction_retains_coverage(
    status: Literal["PARTIAL", "OCR_REQUIRED"],
) -> None:
    verified = result(status=status)
    timeline = build_chronology((verified,), matter_id="matter")
    assert not timeline.dated_groups and not timeline.unresolved_events
    assert timeline.coverage == (verified.coverage,)
    assert timeline.coverage[0].original.warnings == ("Synthetic warning",)
    assert timeline.empty_events_mean == "NO_VERIFIED_EVENT_CANDIDATES"


def test_validation_boundary_rejects_invalid_states_and_malformed_records() -> None:
    original = event("account", date(2020, 1, 1))
    for bad in (
        original.model_copy(update={"verification_state": "UNVERIFIED"}),
        original.model_copy(
            update={
                "temporal": original.temporal.model_copy(
                    update={"verification_state": "INVALID"}
                )
            }
        ),
        original.model_copy(update={"revision": 0}),
    ):
        with pytest.raises(ValueError):
            build_chronology((result(bad),), matter_id="matter")
    with pytest.raises(ValueError, match="matter identity"):
        build_chronology((), matter_id=" ")
    assert build_chronology((), matter_id="matter").coverage == ()

"""Structured extraction candidates remain attributed and source-version scoped."""

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from lextrace.investigations.contracts import (
    AttributedAssertion,
    EntityMention,
    EventParticipant,
    EvidenceReference,
    ExtractionCoverage,
    ExtractionSpan,
    InterpretationBasis,
    InvestigationEvent,
    ReviewState,
    SkippedSpan,
    TemporalReference,
)


@pytest.fixture
def evidence() -> EvidenceReference:
    return EvidenceReference(
        matter_id="matter",
        document_id="document",
        version_id="version",
        original_content_hash="a" * 64,
        extracted_text_hash="b" * 64,
        parser="synthetic",
        parser_version="1",
        extraction_settings_hash="c" * 64,
        section_id="section",
        start=0,
        end=4,
        exact_quote="Jo 😀",
    )


@pytest.fixture
def ownership() -> dict[str, object]:
    return dict(
        matter_id="matter",
        document_id="document",
        version_id="version",
        run_id="run",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        generator="fake-extractor-v1",
    )


@pytest.fixture
def sourced(
    ownership: dict[str, object], evidence: EvidenceReference
) -> dict[str, object]:
    return ownership | {"evidence_references": (evidence,)}


def time_record(sourced: dict[str, object], **changes: object) -> TemporalReference:
    return TemporalReference.model_validate(
        sourced | {"temporal_id": "time", "kind": "unknown"} | changes
    )


def assertion_record(
    sourced: dict[str, object], **changes: object
) -> AttributedAssertion:
    return AttributedAssertion.model_validate(
        sourced
        | dict(
            assertion_id="assertion",
            normalized_proposition="Jo allegedly left.",
            attribution_status="unresolved",
            modality="alleged",
        )
        | changes
    )


def test_valid_candidates_and_strict_roundtrips(
    sourced: dict[str, object], evidence: EvidenceReference
) -> None:
    mention = EntityMention.model_validate(
        sourced | dict(mention_id="mention", entity_type="person", mention_text="Jo 😀")
    )
    temporal = time_record(
        sourced,
        kind="exact",
        raw_expression="January 1, 2026",
        earliest=date(2026, 1, 1),
        latest=date(2026, 1, 1),
        precision="day",
    )
    second = EvidenceReference.model_validate(
        evidence.model_dump() | {"start": 8, "end": 12}
    )
    assertion = assertion_record(
        sourced,
        evidence_references=(evidence, second),
        attributed_to="witness A",
        attribution_status="known",
        dispute_status="disputed",
        uncertainty="Speaker identity has not been independently confirmed.",
        stated_time=temporal,
    )
    event = InvestigationEvent.model_validate(
        sourced
        | dict(
            event_id="event",
            description="Reported departure",
            participants=(EventParticipant(entity_id="entity", role="reported actor"),),
            fact_ids=(assertion.assertion_id,),
            temporal=temporal,
            interpretation_basis=InterpretationBasis.INFERRED_INTERPRETATION,
        )
    )
    for record in (mention, temporal, assertion, event):
        assert type(record).model_validate(record.model_dump()) == record
        assert type(record).model_validate_json(record.model_dump_json()) == record
        with pytest.raises(ValidationError):
            record.matter_id = "changed"
        with pytest.raises(ValidationError):
            type(record).model_validate(record.model_dump() | {"extra": True})
    assert assertion.modality == "alleged"
    assert assertion.verification_state == "UNVERIFIED"
    assert assertion.review_state == ReviewState.PROPOSED
    assert len(assertion.evidence_references) == 2


def test_missing_partial_relative_dates(sourced: dict[str, object]) -> None:
    assert time_record(sourced).earliest is None
    approximate = time_record(
        sourced,
        kind="approximate",
        raw_expression="around March 2020",
        earliest=date(2020, 3, 1),
        latest=date(2020, 3, 31),
        precision="month",
    )
    assert approximate.earliest != approximate.latest
    unbounded = time_record(sourced, kind="approximate", raw_expression="long ago")
    relative = time_record(sourced, kind="relative", raw_expression="the next day")
    interval = time_record(
        sourced,
        kind="interval",
        raw_expression="March 1 through March 3, 2020",
        earliest=date(2020, 3, 1),
        latest=date(2020, 3, 3),
        precision="day",
    )
    for record in (approximate, unbounded, relative, interval):
        assert TemporalReference.model_validate_json(record.model_dump_json()) == record
    assert relative.earliest is None
    assert assertion_record(sourced).attributed_to is None


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "exact", "raw_expression": "today"},
        {"earliest": date(2020, 1, 1)},
        {"earliest": date(2020, 1, 1), "latest": date(2020, 1, 2)},
        {"kind": "relative", "raw_expression": "later", "precision": "day"},
        {
            "kind": "interval",
            "raw_expression": "January to February",
            "earliest": date(2020, 2, 1),
            "latest": date(2020, 1, 1),
        },
        {"kind": "interval"},
        {"kind": "approximate"},
        {
            "kind": "exact",
            "raw_expression": "January",
            "earliest": date(2020, 1, 1),
            "latest": date(2020, 1, 31),
            "precision": "month",
        },
        {"earliest": "2020-01-01"},
    ],
)
def test_contradictory_times(
    sourced: dict[str, object], changes: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        time_record(sourced, **changes)


@pytest.mark.parametrize("field", ["matter_id", "document_id", "version_id"])
def test_wrong_evidence_ownership(sourced: dict[str, object], field: str) -> None:
    with pytest.raises(ValidationError):
        assertion_record(sourced | {field: "other"})


@pytest.mark.parametrize("field", ["matter_id", "document_id", "version_id", "run_id"])
def test_wrong_nested_ownership(sourced: dict[str, object], field: str) -> None:
    changed = sourced | {field: "other"}
    if field != "run_id":
        refs = sourced["evidence_references"]
        assert isinstance(refs, tuple)
        changed["evidence_references"] = tuple(
            EvidenceReference.model_validate(ref.model_dump() | {field: "other"})
            for ref in refs
        )
    temporal = time_record(changed)
    with pytest.raises(ValidationError):
        assertion_record(sourced, stated_time=temporal)
    with pytest.raises(ValidationError):
        InvestigationEvent.model_validate(
            sourced | dict(event_id="event", description="Departure", temporal=temporal)
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"evidence_references": ()},
        {"attribution_status": "known"},
        {"attributed_to": "Jo"},
        {"subject": "Jo"},
        {"revision": True},
        {"revision": "1"},
        {"created_at": datetime(2020, 1, 1)},
        {"interpretation_basis": InterpretationBasis.REVIEWED_FINDING},
        {"review_state": ReviewState.ACCEPTED},
        {"dispute_status": "proven"},
    ],
)
def test_malformed_assertions(
    sourced: dict[str, object], changes: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        assertion_record(sourced, **changes)


def test_review_is_separate_from_provenance(sourced: dict[str, object]) -> None:
    record = assertion_record(
        sourced,
        interpretation_basis=InterpretationBasis.REVIEWED_FINDING,
        review_state=ReviewState.ACCEPTED,
        review_decision_id="decision",
    )
    assert record.verification_state == "UNVERIFIED"
    assert record.dispute_status == "unresolved"


def test_conflicting_representation_and_nonexact_mention(
    sourced: dict[str, object], evidence: EvidenceReference
) -> None:
    other = EvidenceReference.model_validate(
        evidence.model_dump() | {"parser_version": "2"}
    )
    with pytest.raises(ValidationError):
        assertion_record(sourced, evidence_references=(evidence, other))
    with pytest.raises(ValidationError):
        EntityMention.model_validate(
            sourced
            | dict(mention_id="mention", entity_type="person", mention_text="Joseph")
        )


def coverage_record(
    ownership: dict[str, object], **changes: object
) -> ExtractionCoverage:
    return ExtractionCoverage.model_validate(
        ownership
        | dict(coverage_id="coverage", text_length=10, status="PARTIAL")
        | changes
    )


def test_incomplete_and_ocr_coverage(ownership: dict[str, object]) -> None:
    partial = coverage_record(
        ownership,
        processed_windows=(ExtractionSpan(start=0, end=5, page_start=1, page_end=1),),
        processed_pages=(1,),
        skipped_spans=(SkippedSpan(start=5, end=10, reason="BUDGET_EXHAUSTED"),),
        excluded_pages=(2,),
        warnings=("Page 2 unavailable",),
        incomplete_reasons=("BUDGET_EXHAUSTED",),
    )
    ocr = coverage_record(
        ownership,
        text_length=0,
        status="OCR_REQUIRED",
        excluded_pages=(1, 2),
        incomplete_reasons=("OCR_REQUIRED",),
    )
    complete = coverage_record(
        ownership,
        status="COMPLETE",
        processed_windows=(
            ExtractionSpan(start=0, end=7),
            ExtractionSpan(start=5, end=10),
        ),
    )
    for record in (partial, ocr, complete):
        assert (
            ExtractionCoverage.model_validate_json(record.model_dump_json()) == record
        )


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"status": "COMPLETE"},
        {"status": "COMPLETE", "incomplete_reasons": ("OCR_REQUIRED",)},
        {
            "status": "OCR_REQUIRED",
            "processed_pages": (1,),
            "incomplete_reasons": ("OCR_REQUIRED",),
        },
        {
            "incomplete_reasons": ("LIMIT",),
            "processed_pages": (1,),
            "excluded_pages": (1,),
        },
        {
            "incomplete_reasons": ("LIMIT",),
            "processed_windows": (ExtractionSpan(start=0, end=11),),
        },
        {
            "incomplete_reasons": ("LIMIT",),
            "processed_windows": (ExtractionSpan(start=0, end=5),),
            "skipped_spans": (SkippedSpan(start=4, end=10, reason="LIMIT"),),
        },
    ],
)
def test_invalid_coverage(
    ownership: dict[str, object], changes: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        coverage_record(ownership, **changes)


@pytest.mark.parametrize(
    "values",
    [
        {"start": 2, "end": 2},
        {"start": -1, "end": 2},
        {"start": 0, "end": 2, "page_start": 1},
        {"start": 0, "end": 2, "page_start": 2, "page_end": 1},
    ],
)
def test_invalid_coverage_spans(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ExtractionSpan.model_validate(values)

"""Pure review transitions against deterministic synthetic evidence."""

from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from lextrace.investigations.contracts import AttributedAssertion, EntityMention
from lextrace.investigations.review import (
    DateCorrection,
    ReviewDecision,
    apply_review,
    initial_review,
    target_of,
)
from tests.unit.test_investigation_chronology import event


def command(record: object, **updates: object) -> ReviewDecision:
    from typing import cast

    from lextrace.investigations.review import Candidate

    values: dict[str, object] = {
        "decision_id": "decision",
        "reviewer_id": "reviewer",
        "timestamp": datetime(2020, 1, 1, tzinfo=UTC),
        "target": target_of(cast(Candidate, record)),
        "expected_revision": 1,
        "decision": "ACCEPT",
    }
    values.update(updates)
    return ReviewDecision.model_validate(values)


@pytest.mark.parametrize(
    "decision,state",
    [("ACCEPT", "ACCEPTED"), ("REJECT", "REJECTED"), ("DEFER", "DEFERRED")],
)
def test_decisions_and_history(decision: str, state: str) -> None:
    original = event("event", date(2020, 1, 1))
    first = initial_review(original)
    decision_record = command(original, decision=decision)
    second = apply_review(first, decision_record)
    assert second.review_state == state
    assert first.revision == 1 and second.revision == 2
    assert second.original == original
    assert second.original.interpretation_basis == "SOURCE_ASSERTION"
    assert apply_review(second, decision_record) == second
    with pytest.raises(ValueError, match="identity conflicts"):
        apply_review(
            second, decision_record.model_copy(update={"reviewer_id": "other"})
        )
    with pytest.raises(ValueError, match="revision conflicts"):
        apply_review(second, command(original, decision_id="competing"))
    with pytest.raises(ValueError, match="ownership"):
        apply_review(first, command(event("different", date(2020, 1, 1))))


def test_correction_and_explicit_mention_resolution() -> None:
    original = event("event", date(2020, 1, 1))
    correction = DateCorrection(
        kind="exact",
        earliest=date(2020, 2, 1),
        latest=date(2020, 2, 1),
        precision="day",
    )
    edited = apply_review(
        initial_review(original),
        command(original, decision="EDIT", date_correction=correction),
    )
    assert edited.review_state == "PROPOSED"
    assert edited.original.temporal == original.temporal  # type: ignore[union-attr]
    interpreted = edited.interpreted_time()
    assert interpreted is not None and interpreted.earliest == date(2020, 2, 1)
    assert edited.original.evidence_references == original.evidence_references
    mention = EntityMention(
        **{
            key: value
            for key, value in original.model_dump().items()
            if key in EntityMention.model_fields
        },
        mention_id="m",
        entity_type="person",
        mention_text="account",
    )
    merged = apply_review(
        initial_review(mention),
        command(mention, decision="EDIT", resolved_entity_id="canonical-person"),
    )
    assert merged.resolved_entity_id == "canonical-person"
    assert merged.original == mention and mention.entity_id is None
    assertion = AttributedAssertion(
        **{
            key: value
            for key, value in original.model_dump().items()
            if key in AttributedAssertion.model_fields
        },
        assertion_id="a",
        normalized_proposition="source alleges",
        attribution_status="unknown",
        modality="alleged",
    )
    assert (
        apply_review(initial_review(assertion), command(assertion)).review_state
        == "ACCEPTED"
    )


def test_invalid_commands() -> None:
    original = event("event", date(2020, 1, 1))
    for updates in (
        {"decision": "INVALID"},
        {"decision": "EDIT"},
        {"reviewer_id": " "},
        {"timestamp": datetime(2020, 1, 1, tzinfo=timezone(timedelta(hours=1)))},
        {"resolved_entity_id": "person"},
    ):
        with pytest.raises(ValidationError):
            command(original, **updates)
    with pytest.raises(ValidationError):
        DateCorrection(
            kind="exact",
            earliest=date(2020, 2, 1),
            latest=date(2020, 1, 1),
            precision="day",
        )


def test_reviewed_chronology_preserves_unknown_source_and_exclusions() -> None:
    from lextrace.investigations.reviewed_chronology import build_reviewed_chronology

    unknown = event("unknown", kind="unknown", precision="unknown")
    unknown = unknown.model_copy(
        update={
            "temporal": unknown.temporal.model_copy(update={"raw_expression": None})
        }
    )
    correction = DateCorrection(
        kind="exact",
        earliest=date(2020, 2, 1),
        latest=date(2020, 2, 1),
        precision="day",
    )
    edited = apply_review(
        initial_review(unknown),
        command(unknown, decision="EDIT", date_correction=correction),
    )
    accepted = apply_review(
        edited, command(unknown, decision_id="accept-edit", expected_revision=2)
    )
    later = event("later", date(2021, 1, 1))
    rejected = apply_review(initial_review(later), command(later, decision="REJECT"))
    chronology = build_reviewed_chronology(
        (rejected, accepted), matter_id="matter", require_accepted=True
    )
    assert chronology.dated_groups[0].earliest == date(2020, 2, 1)
    assert chronology.dated_groups[0].events == (accepted,)
    assert chronology.excluded_events == (rejected,)
    assert accepted.original == unknown
    assert unknown.temporal.raw_expression is None
    assert unknown.temporal.earliest is None
    assert build_reviewed_chronology(
        (initial_review(unknown),), matter_id="matter"
    ).unresolved_events

"""Synthetic M6A opportunities; no network, models, storage or truth inference."""

from dataclasses import replace
from datetime import date
from typing import Literal

import pytest
from pydantic import ValidationError

from lextrace.investigations.contracts import (
    AttributedAssertion,
    EntityMention,
    SourceIdentity,
)
from lextrace.investigations.inconsistency_candidates import (
    CandidateBudgets,
    CandidateInput,
    CandidatePair,
    CandidateSelection,
    select_inconsistency_candidates,
)
from lextrace.investigations.review import (
    DateCorrection,
    RecordDependency,
    ReviewRevision,
    apply_review,
    initial_review,
    target_of,
)
from tests.unit.test_investigation_chronology import event, result
from tests.unit.test_investigation_review import command


def accept(revision: ReviewRevision) -> ReviewRevision:
    return apply_review(
        revision,
        command(
            revision.original,
            expected_revision=revision.revision,
            decision_id=f"accept-{revision.revision}",
        ),
    )


def item(
    identity: str,
    *,
    document: str = "document",
    assertion: bool = True,
    speaker: str = "speaker",
    negative: bool = False,
    modality: Literal["asserted", "alleged"] = "asserted",
) -> CandidateInput:
    original = event(identity, date(2020, 1, 1), document_id=document)
    mention = EntityMention(
        **{
            k: v
            for k, v in original.model_dump().items()
            if k in EntityMention.model_fields
        },
        mention_id="mention-" + identity,
        entity_type="person",
        mention_text="account",
    )
    resolved = apply_review(
        initial_review(mention),
        command(mention, decision="EDIT", resolved_entity_id="entity"),
    )
    reviewed_mention = accept(resolved)
    if assertion:
        account = AttributedAssertion(
            **{
                k: v
                for k, v in original.model_dump().items()
                if k in AttributedAssertion.model_fields
            },
            assertion_id=identity,
            normalized_proposition="Exact source account",
            attribution_status="known",
            attributed_to=speaker,
            subject="subject",
            predicate="delivered",
            object="package",
            polarity="negative" if negative else "positive",
            modality=modality,
            stated_time=original.temporal,
        )
        return CandidateInput(
            revision=accept(initial_review(account)),
            entity_mentions=(reviewed_mention,),
        )
    return CandidateInput(
        revision=accept(initial_review(original)), entity_mentions=(reviewed_mention,)
    )


def select(
    *items: CandidateInput,
    budgets: CandidateBudgets | None = None,
    current: tuple[RecordDependency, ...] | None = None,
    require_accepted: bool = True,
) -> CandidateSelection:
    revisions = {
        target_of(r.original).model_dump_json(): r
        for i in items
        for r in (i.revision, *i.entity_mentions)
    }
    sources = {
        SourceIdentity.model_validate(
            {k: getattr(ref, k) for k in SourceIdentity.model_fields}
        ).model_dump_json(): SourceIdentity.model_validate(
            {k: getattr(ref, k) for k in SourceIdentity.model_fields}
        )
        for r in revisions.values()
        for ref in r.original.evidence_references
    }
    verified = replace(
        result(), valid_candidates=tuple(r.original for r in revisions.values())
    )
    deps = tuple(
        RecordDependency(target=target_of(r.original), revision=r.revision)
        for r in revisions.values()
    )
    return select_inconsistency_candidates(
        tuple(items),
        matter_id="matter",
        sources=tuple(sources.values()),
        verified=(verified,),
        current=deps if current is None else current,
        budgets=budgets,
        require_accepted=require_accepted,
    )


@pytest.mark.parametrize("cross_source", [False, True])
@pytest.mark.parametrize("speaker", ["speaker", "other"])
def test_exact_accounts_preserved(cross_source: bool, speaker: str) -> None:
    a = item("a")
    b = item(
        "b",
        document="other" if cross_source else "document",
        speaker=speaker,
        negative=True,
        modality="alleged",
    )
    output = select(a, b)
    pair = output.pairs[0]
    assert pair.status == "CANDIDATE_FOR_REVIEW"
    assert "OPPOSING_POLARITY" in pair.reasons
    assert "OVERLAPPING_DATES" in pair.reasons
    assert (
        "SAME_SPEAKER" if speaker == "speaker" else "DIFFERENT_SPEAKERS"
    ) in pair.cautions
    assert "NON_ASSERTED_MODALITY" in pair.cautions
    assert pair.left.revision.original == a.revision.original
    assert (
        pair.right.revision.original.evidence_references
        == b.revision.original.evidence_references
    )
    assert len(pair.dependencies) == 4
    assert output == select(b, a, b)
    assert "account" not in str(output.diagnostics)
    with pytest.raises(ValidationError):
        pair.status = "PROVEN"  # type: ignore[assignment]
    with pytest.raises(ValidationError):
        CandidatePair.model_validate({**pair.model_dump(), "pair_id": "a" * 64})


@pytest.mark.parametrize("kind", ["unknown", "relative", "approximate", "interval"])
def test_ambiguous_dates_remain_ambiguous(kind: str) -> None:
    a, b = item("a", assertion=False), item("b", assertion=False)
    original = b.revision.original
    assert hasattr(original, "temporal")
    temporal = event("time", kind="unknown", precision="unknown").temporal
    if kind == "relative":
        temporal = temporal.model_copy(update={"kind": "relative"})
    if kind in ("approximate", "interval"):
        temporal = temporal.model_copy(
            update={
                "kind": kind,
                "earliest": date(2020, 1, 1),
                "latest": date(2020, 1, 31),
                "precision": "month",
            }
        )
    updated = original.model_copy(update={"temporal": temporal})
    b = b.model_copy(update={"revision": accept(initial_review(updated))})
    pair = select(a, b).pairs[0]
    assert "EXACT_DATE_DIFFERENCE" not in pair.reasons
    assert (
        "INSUFFICIENT_TIME" if kind in ("unknown", "relative") else "AMBIGUOUS_TIME"
    ) in pair.cautions


def test_date_interpretations_and_event_scope() -> None:
    a, b = item("a", assertion=False), item("b", assertion=False)
    edited = apply_review(
        b.revision,
        command(
            b.revision.original,
            decision="EDIT",
            expected_revision=2,
            decision_id="date-edit",
            date_correction=DateCorrection(
                kind="exact",
                earliest=date(2020, 1, 2),
                latest=date(2020, 1, 2),
                precision="day",
            ),
        ),
    )
    b = b.model_copy(update={"revision": accept(edited)})
    pair = select(a, b).pairs[0]
    assert "EXACT_DATE_DIFFERENCE" in pair.reasons
    assert "EVENT_SCOPE_REQUIRES_REVIEW" in pair.cautions
    assert select(a, b, budgets=CandidateBudgets(date_window_days=0)).pairs == ()
    different = b.revision.original.model_copy(update={"description": "another event"})
    b = b.model_copy(update={"revision": accept(initial_review(different))})
    assert select(a, b).diagnostics[0].code == "DIFFERENT_DESCRIPTOR"


def test_insufficient_comparison_information() -> None:
    a, b = item("a"), item("b")
    assert (
        select(a, b.model_copy(update={"entity_mentions": ()})).diagnostics[0].code
        == "NO_SHARED_REVIEWED_ENTITY"
    )
    original = b.revision.original.model_copy(
        update={"subject": None, "predicate": None, "object": None}
    )
    b = b.model_copy(update={"revision": accept(initial_review(original))})
    assert select(a, b).diagnostics[0].code == "INSUFFICIENT_DESCRIPTOR"


@pytest.mark.parametrize("decision", ["REJECT", "DEFER", "PROPOSED"])
def test_review_exclusions(decision: str) -> None:
    a, b = item("a"), item("b")
    revision = initial_review(b.revision.original)
    if decision != "PROPOSED":
        revision = apply_review(revision, command(revision.original, decision=decision))
    b = b.model_copy(update={"revision": revision})
    assert not select(a, b).pairs
    if decision == "PROPOSED":
        assert select(a, b, require_accepted=False).pairs
    assert not select(a, item("b"), current=()).pairs


@pytest.mark.parametrize(
    "budget,code",
    [
        (CandidateBudgets(max_inputs=2), "INPUT_BUDGET"),
        (CandidateBudgets(max_comparisons=1), "COMPARISON_BUDGET"),
        (CandidateBudgets(max_pairs=1), "PAIR_BUDGET"),
        (CandidateBudgets(max_pairs_per_source=1), "SOURCE_BUDGET"),
        (CandidateBudgets(max_pairs_per_target=1), "TARGET_BUDGET"),
    ],
)
def test_budgets(budget: CandidateBudgets, code: str) -> None:
    items = tuple(item(str(i)) for i in range(4))
    output = select(*items, budgets=budget)
    assert output.partial
    assert code in {d.code for d in output.diagnostics}
    assert output == select(*reversed(items), budgets=budget)
    assert output.comparisons <= budget.max_comparisons
    assert len(output.pairs) <= budget.max_pairs


def test_scope_and_provenance_fail_closed() -> None:
    a, b = item("a"), item("b")
    revisions = (a.revision, b.revision, *a.entity_mentions, *b.entity_mentions)
    deps = tuple(
        RecordDependency(target=target_of(r.original), revision=r.revision)
        for r in revisions
    )
    assert not select_inconsistency_candidates(
        (a, b),
        matter_id="matter",
        sources=(),
        verified=(
            replace(result(), valid_candidates=tuple(r.original for r in revisions)),
        ),
        current=deps,
    ).pairs
    assert not select_inconsistency_candidates(
        (a, b), matter_id="matter", sources=(), verified=(), current=deps
    ).pairs


def test_current_revision_and_identity_link_gates() -> None:
    a, b = item("a"), item("b")
    # Accepted historical review is excluded when the current revision is newer.
    revisions = (a.revision, b.revision, *a.entity_mentions, *b.entity_mentions)
    deps = tuple(
        RecordDependency(
            target=target_of(r.original),
            revision=r.revision + (1 if r == b.revision else 0),
        )
        for r in revisions
    )
    assert not select(a, b, current=deps).pairs
    # Unaccepted identity resolution cannot serve as a shared identity signal.
    mention = b.entity_mentions[0]
    proposed = apply_review(
        initial_review(mention.original),
        command(mention.original, decision="EDIT", resolved_entity_id="entity"),
    )
    assert not select(
        a, b.model_copy(update={"entity_mentions": (proposed,)}), require_accepted=False
    ).pairs
    different = apply_review(
        mention,
        command(
            mention.original,
            expected_revision=3,
            decision="EDIT",
            decision_id="other-identity",
            resolved_entity_id="different",
        ),
    )
    different = accept(different)
    assert not select(a, b.model_copy(update={"entity_mentions": (different,)})).pairs


def test_conflicting_duplicate_payload_fails_safely() -> None:
    a = item("a")
    altered = a.model_copy(
        update={
            "revision": a.revision.model_copy(
                update={
                    "original": a.revision.original.model_copy(
                        update={"predicate": "private text"}
                    )
                }
            )
        }
    )
    with pytest.raises(ValueError, match="Conflicting upstream revision identity"):
        select(a, altered)


def test_missing_time_and_unknown_attribution() -> None:
    a, b = item("a"), item("b")
    original = b.revision.original.model_copy(
        update={
            "stated_time": None,
            "attribution_status": "unknown",
            "attributed_to": None,
        }
    )
    b = b.model_copy(update={"revision": accept(initial_review(original))})
    pair = select(a, b).pairs[0]
    assert "INSUFFICIENT_TIME" in pair.cautions
    assert "UNKNOWN_ATTRIBUTION" in pair.cautions


def test_mixed_event_assertion_exact_descriptor() -> None:
    a, b = item("a"), item("b", assertion=False)
    original = b.revision.original.model_copy(update={"description": "delivered"})
    b = b.model_copy(update={"revision": accept(initial_review(original))})
    assert select(a, b).pairs[0].status == "CANDIDATE_FOR_REVIEW"

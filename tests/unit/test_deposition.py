# ruff: noqa: F811 -- shared synthetic pytest fixtures
"""Deterministic witness/question grounding over real reviewed snapshot inputs."""

from dataclasses import replace

import pytest

from lextrace.deposition.contracts import PlanError, PlanView, ReviewPlan
from lextrace.deposition.generation import generate_basis
from lextrace.deposition.service import DepositionService
from lextrace.investigations.inconsistency_investigation import evidence_id
from lextrace.investigations.workflow import ApprovedInvestigationSnapshot
from tests.deposition_fixtures import deposition as deposition  # noqa: F401
from tests.unit.test_investigation_durable import Harness
from tests.unit.test_investigation_durable import durable as durable  # noqa: F401
from tests.unit.test_investigation_workflow import setup as memory_setup  # noqa: F401


def create(service: DepositionService, harness: Harness) -> PlanView:
    thread = service.runtime.read("SELECT thread_id FROM investigation_runs")[0][0]
    snapshot = service.workflow.approved_snapshot(thread)
    return service.create(
        harness.runtime.matter, thread, snapshot.snapshot_id, "create-1", "reviewer"
    )


def test_dossiers_keep_same_name_source_accounts_separate(
    deposition: DepositionService, durable: Harness
) -> None:
    plan = create(deposition, durable)
    basis = plan.basis
    assert len(basis.witnesses) == 2
    assert basis.witnesses[0].label == basis.witnesses[1].label
    assert len({w.witness_id for w in basis.witnesses}) == 2
    assert all(len(w.accounts) == 1 for w in basis.witnesses)
    assert all(
        "SPEAKER_IDENTITIES_NOT_MERGED_BY_NAME" in w.uncertainties
        for w in basis.witnesses
    )
    registry = {item.evidence_id: item.reference for item in basis.evidence}
    for question in basis.questions:
        assert question.wording.endswith("?")
        assert question.evidence_ids
        for identity in question.evidence_ids:
            ref = registry[identity]
            assert evidence_id(ref) == identity
            text = durable.runtime.store.document_text(ref.matter_id, ref.document_id)
            assert text[ref.start : ref.end] == ref.exact_quote
    confrontation = [
        q for q in basis.questions if q.category == "POTENTIAL_INCONSISTENCY"
    ]
    assert confrontation
    assert all(
        len({registry[i].document_id for i in q.evidence_ids}) == 2
        for q in confrontation
    )
    assert all("proven contradiction" in q.rationale for q in confrontation)
    assert all(w.chronology for w in basis.witnesses)


@pytest.mark.parametrize(
    "change", ["partial", "quarantine", "empty", "unknown", "unsupported"]
)
def test_limited_and_blocked_without_fabrication(
    deposition: DepositionService, durable: Harness, change: str
) -> None:
    with deposition.published(
        durable.runtime.matter,
        deposition.runtime.read("SELECT thread_id FROM investigation_runs")[0][0],
    ) as (_, context):
        artifacts = replace(context.artifacts)
        revisions = context.revisions
        if change == "partial":
            artifacts.verified = tuple(
                replace(v, coverage=replace(v.coverage, status="PARTIAL"))
                for v in artifacts.verified
            )
        elif change == "quarantine":
            from lextrace.investigations.extraction_verification import (
                VerificationQuarantine,
            )

            bad = revisions[0].original.model_copy(
                update={
                    "evidence_references": (
                        revisions[0]
                        .original.evidence_references[0]
                        .model_copy(update={"exact_quote": "FORGED QUOTE"}),
                    )
                }
            )
            artifacts.verified = tuple(
                replace(
                    v,
                    quarantined_candidates=(
                        VerificationQuarantine(0, bad, ("QUOTE_MISMATCH",)),
                    ),
                )
                for v in artifacts.verified
            )
        elif change == "empty":
            revisions = ()
        elif change == "unknown":
            from lextrace.investigations.contracts import AttributedAssertion

            revisions = tuple(
                r.model_copy(
                    update={
                        "original": r.original.model_copy(
                            update={
                                "attribution_status": "unknown",
                                "attributed_to": None,
                            }
                        )
                    }
                )
                if isinstance(r.original, AttributedAssertion)
                else r
                for r in revisions
            )
        else:
            assert artifacts.result is not None
            artifacts.result = artifacts.result.model_copy(
                update={
                    "findings": tuple(
                        f.model_copy(update={"verification": "INSUFFICIENT"})
                        for f in artifacts.result.findings
                    )
                }
            )
        basis = generate_basis(context.snapshot, artifacts, revisions, "a" * 64)
        assert "FORGED QUOTE" not in basis.model_dump_json()
        if change in ("empty", "unknown"):
            assert basis.outcome == "BLOCKED"
            assert not basis.questions and not basis.witnesses
        elif change != "unsupported":
            assert basis.outcome == "LIMITED"
        else:
            assert basis.outcome == "LIMITED"
            assert not any(
                q.category in ("POTENTIAL_INCONSISTENCY", "FOLLOW_UP")
                for q in basis.questions
            )


def test_question_edits_must_keep_related_evidence(
    deposition: DepositionService, durable: Harness
) -> None:
    view = create(deposition, durable)
    question = next(q for q in view.current.questions if q.category == "FOUNDATION")
    other = next(
        q
        for q in view.current.questions
        if q.witness_id != question.witness_id and q.category == "FOUNDATION"
    )
    wrong = question.model_copy(update={"evidence_ids": other.evidence_ids})
    with pytest.raises(PlanError, match="INVALID_EVIDENCE"):
        deposition.validate_questions(
            view.basis,
            tuple(
                wrong if q.question_id == question.question_id else q
                for q in view.current.questions
            ),
        )
    pair = next(
        q for q in view.current.questions if q.category == "POTENTIAL_INCONSISTENCY"
    )
    wrong = pair.model_copy(update={"evidence_ids": pair.evidence_ids[:1]})
    with pytest.raises(PlanError, match="INVALID_EVIDENCE"):
        deposition.validate_questions(
            view.basis,
            tuple(
                wrong if q.question_id == pair.question_id else q
                for q in view.current.questions
            ),
        )


def test_review_is_idempotent_and_preserves_signed_snapshot(
    deposition: DepositionService, durable: Harness
) -> None:
    view = create(deposition, durable)
    basis = view.basis
    signed = durable.workflow.approved_snapshot(basis.thread_id)
    request = ReviewPlan(
        decision_id="approve",
        expected_revision=1,
        action="APPROVE",
        questions=view.current.questions,
        confirm=True,
        acknowledge_limitations=True,
    )
    approved = deposition.review(
        basis.matter_id, basis.thread_id, basis.plan_id, request, "human"
    )
    assert approved.current.status == "APPROVED" and approved.current.revision == 2
    assert (
        deposition.review(
            basis.matter_id, basis.thread_id, basis.plan_id, request, "human"
        )
        == approved
    )
    assert durable.workflow.approved_snapshot(basis.thread_id) == signed
    assert len(deposition.history(basis.matter_id, basis.thread_id, basis.plan_id)) == 2
    with pytest.raises(PlanError, match="RETRY_CONFLICT"):
        deposition.review(
            basis.matter_id, basis.thread_id, basis.plan_id, request, "other-human"
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("matter_id", "foreign"),
        ("document_id", "missing"),
        ("version_id", "stale"),
        ("section_id", "absent"),
        ("start", 99999),
        ("exact_quote", "fabricated"),
    ],
)
def test_reference_guard_rejects_non_snapshot_evidence(
    deposition: DepositionService, durable: Harness, field: str, value: object
) -> None:
    view = create(deposition, durable)
    snapshot = deposition.workflow.approved_snapshot(view.basis.thread_id)
    ref = view.basis.evidence[0].reference.model_copy(update={field: value})
    with pytest.raises(PlanError):
        deposition.validate_reference(ref, snapshot)


def test_working_artifacts_are_not_a_deposition_source(
    deposition: DepositionService, durable: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = deposition.workflow.approved_snapshot

    def mutated(thread: str) -> ApprovedInvestigationSnapshot:
        snapshot = original(thread)
        deposition.workflow.artifacts[thread].verified = ()
        return snapshot

    monkeypatch.setattr(deposition.workflow, "approved_snapshot", mutated)
    view = create(deposition, durable)
    assert len(view.basis.witnesses) == 2
    assert view.basis.questions

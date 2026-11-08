"""M7A actual in-memory LangGraph continuation, entirely offline."""

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import TypeVar

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel

from lextrace.investigations.evidence_search import ApprovedVersion
from lextrace.investigations.extraction_verification import ExtractionVerificationResult
from lextrace.investigations.repository import InvestigationRepository
from lextrace.investigations.review import target_of
from lextrace.investigations.workflow import (
    HumanApproval,
    HumanEntityLink,
    InvestigationServices,
    InvestigationState,
    InvestigationWorkflow,
    WorkflowBudgets,
)
from lextrace.matter.store import MatterStore
from tests.unit.test_inconsistency_investigation import Runtime
from tests.unit.test_investigation_chronology import result
from tests.unit.test_investigation_review import command


class SeededServices(InvestigationServices):
    """Inject preverified M3 artifacts, preserving real M4/M5/M6 services."""

    extraction_calls = 0
    verified: tuple[ExtractionVerificationResult, ...] = ()

    def extract(
        self, state: InvestigationState
    ) -> tuple[ExtractionVerificationResult, ...]:
        self.extraction_calls += 1
        return self.verified


@pytest.fixture
def setup(
    tmp_path: Path,
) -> Iterator[tuple[Runtime, SeededServices, InvestigationWorkflow, InMemorySaver]]:
    store = MatterStore(tmp_path / "db", tmp_path / "private")
    repo = InvestigationRepository(tmp_path / "db", source_store=store)
    runtime = Runtime(store, repo)
    pair = runtime.pair()
    services = SeededServices(store, repo, runtime.reasoner, runtime.service)
    services.verified = (
        replace(
            result(),
            valid_candidates=tuple(
                r.original
                for i in (pair.left, pair.right)
                for r in (i.revision, *i.entity_mentions)
            ),
        ),
    )
    saver = InMemorySaver()
    workflow = InvestigationWorkflow(services, checkpointer=saver)
    yield runtime, services, workflow, saver
    repo.close()
    store.close()


def approval(state: InvestigationState, **changes: object) -> HumanApproval:
    assert state.review_gate and state.pending_review_token and state.approval_digest
    return HumanApproval.model_validate(
        dict(
            matter_id=state.matter_id,
            run_id=state.run_id,
            thread_id=state.thread_id,
            gate=state.review_gate,
            token=state.pending_review_token,
            digest=state.approval_digest,
            reviewer_id="human",
            decision="APPROVE",
        )
        | changes
    )


def linked(workflow: InvestigationWorkflow, state: InvestigationState) -> HumanApproval:
    revisions = workflow.services.revisions(workflow.artifacts[state.thread_id])
    accounts = [r for r in revisions if target_of(r.original).kind == "assertion"]
    mentions = [r for r in revisions if target_of(r.original).kind == "mention"]
    return approval(
        state,
        entity_links=tuple(
            HumanEntityLink(
                account=target_of(a.original),
                mentions=tuple(
                    target_of(m.original)
                    for m in mentions
                    if m.original.document_id == a.original.document_id
                ),
            )
            for a in accounts
        ),
    )


def test_two_real_interrupts_and_snapshot(
    setup: tuple[Runtime, SeededServices, InvestigationWorkflow, InMemorySaver],
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime, services, workflow, saver = setup
    first = workflow.start(runtime.matter, tuple(runtime.approved))
    assert first.review_gate == "evidence" and first.stage == "WAITING_REVIEW"
    assert workflow.pending_gate(first.thread_id) == first
    assert workflow._graph.get_state(workflow._config(first.thread_id)).interrupts
    second = workflow.resume(linked(workflow, first))
    assert second.review_gate == "findings" and second.stage == "WAITING_REVIEW"
    assert len(second.candidate_ids) == len(second.finding_ids) == 1
    assert runtime.reasoner.calls == runtime.verifier.calls == 1
    final = workflow.resume(approval(second))
    assert final.stage == "APPROVED"
    assert workflow.pending_gate(final.thread_id) is None
    snapshot = workflow.artifacts[final.thread_id].snapshot
    assert snapshot and snapshot.finding_ids == final.finding_ids
    assert snapshot.meaning == "HUMAN_SIGNED_INVESTIGATION_HYPOTHESES"
    findings = workflow.artifacts[final.thread_id].result
    assert findings is not None and findings.findings[0].review_state == "PROPOSED"
    assert (
        services.extraction_calls == runtime.search.calls == runtime.reasoner.calls == 1
    )
    payload = repr(list(saver.list(None))) + caplog.text
    assert "I delivered" not in payload and "I did not deliver" not in payload
    assert "normalized_proposition" not in payload and "exact_quote" not in payload


@pytest.mark.parametrize(
    "field,value",
    [
        ("matter_id", "wrong"),
        ("run_id", "wrong"),
        ("thread_id", "wrong"),
        ("gate", "findings"),
        ("token", "wrong"),
        ("digest", "f" * 64),
    ],
)
@pytest.mark.parametrize("gate", ["evidence", "findings"])
def test_invalid_approval_does_not_consume_gate(
    setup: tuple[Runtime, SeededServices, InvestigationWorkflow, InMemorySaver],
    field: str,
    value: str,
    gate: str,
) -> None:
    runtime, services, workflow, _ = setup
    first = workflow.start(runtime.matter, tuple(runtime.approved))
    if gate == "findings":
        first = workflow.resume(linked(workflow, first))
        if field == "gate":
            value = "evidence"
    calls = runtime.reasoner.calls
    with pytest.raises(ValueError):
        workflow.resume(approval(first, **{field: value}))
    assert workflow.pending_gate(first.thread_id) == first
    assert services.extraction_calls == 1 and runtime.reasoner.calls == calls
    if gate == "evidence":
        assert workflow.resume(linked(workflow, first)).review_gate == "findings"
    else:
        assert workflow.resume(approval(first)).stage == "APPROVED"


@pytest.mark.parametrize("gate", ["evidence", "findings"])
def test_source_change_before_resume(
    setup: tuple[Runtime, SeededServices, InvestigationWorkflow, InMemorySaver],
    gate: str,
) -> None:
    runtime, _, workflow, _ = setup
    state = workflow.start(runtime.matter, tuple(runtime.approved))
    if gate == "findings":
        state = workflow.resume(linked(workflow, state))
    version = runtime.repo.active_document_version(
        runtime.matter, runtime.approved[0].document_id
    )
    assert version
    runtime.repo.create_document_version(
        version.model_copy(update={"version_id": "changed"})
    )
    with pytest.raises(ValueError, match="Stale"):
        workflow.resume(approval(state))
    assert workflow.artifacts[state.thread_id].snapshot is None


def test_review_change_at_gate_two(
    setup: tuple[Runtime, SeededServices, InvestigationWorkflow, InMemorySaver],
) -> None:
    runtime, _, workflow, _ = setup
    first = workflow.start(runtime.matter, tuple(runtime.approved))
    second = workflow.resume(linked(workflow, first))
    revision = workflow.services.revisions(workflow.artifacts[first.thread_id])[0]
    runtime.repo.review(
        command(
            revision.original,
            expected_revision=revision.revision,
            decision_id="stale",
            decision="REJECT",
        )
    )
    with pytest.raises(ValueError, match="Stale"):
        workflow.resume(approval(second))
    assert workflow.artifacts[first.thread_id].snapshot is None


def test_durable_decisions_single_use_no_duplicate_work(
    setup: tuple[Runtime, SeededServices, InvestigationWorkflow, InMemorySaver],
) -> None:
    runtime, services, workflow, _ = setup
    first = workflow.start(runtime.matter, tuple(runtime.approved))
    revision = services.revisions(workflow.artifacts[first.thread_id])[0]
    decision = command(
        revision.original,
        expected_revision=revision.revision,
        decision_id="gate-one",
        reviewer_id="human",
    )
    request = linked(workflow, first).model_copy(
        update={"evidence_decisions": (decision,)}
    )
    second = workflow.resume(request)
    history = runtime.repo.review_history(target_of(revision.original))
    assert len(history) == 3 and history[-1].decision == decision
    with pytest.raises(ValueError):
        workflow.resume(request)
    assert runtime.repo.review_history(target_of(revision.original)) == history
    assert workflow.resume(approval(second)).stage == "APPROVED"
    with pytest.raises(ValueError):
        workflow.resume(approval(second))
    assert services.extraction_calls == runtime.reasoner.calls == 1


@pytest.mark.parametrize("decision", ["REJECT", "DEFER", "EDIT"])
def test_unaccepted_inputs_block_investigation(
    setup: tuple[Runtime, SeededServices, InvestigationWorkflow, InMemorySaver],
    decision: str,
) -> None:
    from lextrace.investigations.review import DateCorrection

    runtime, services, workflow, _ = setup
    first = workflow.start(runtime.matter, tuple(runtime.approved))
    revision = services.revisions(workflow.artifacts[first.thread_id])[0]
    changes: dict[str, object] = {}
    if decision == "EDIT":
        changes["date_correction"] = DateCorrection(kind="unknown")
    cmd = command(
        revision.original,
        decision=decision,
        reviewer_id="human",
        expected_revision=revision.revision,
        decision_id="not-accepted",
        **changes,
    )
    state = workflow.resume(approval(first, evidence_decisions=(cmd,)))
    assert state.stage == "BLOCKED" and state.errors == ("UNACCEPTED_INPUT",)
    assert runtime.reasoner.calls == 0


@pytest.mark.parametrize("gate", ["evidence", "findings"])
def test_rejection_and_cancellation(
    setup: tuple[Runtime, SeededServices, InvestigationWorkflow, InMemorySaver],
    gate: str,
) -> None:
    runtime, _, workflow, _ = setup
    state = workflow.start(runtime.matter, tuple(runtime.approved))
    if gate == "findings":
        state = workflow.resume(linked(workflow, state))
    assert workflow.resume(approval(state, decision="REJECT")).stage == "BLOCKED"
    state = workflow.start(runtime.matter, tuple(runtime.approved))
    assert workflow.cancel(state.thread_id).stage == "CANCELED"
    with pytest.raises(ValueError):
        workflow.resume(approval(state))
    assert workflow.artifacts[state.thread_id].snapshot is None


def test_transition_budget_and_global_model_budget(
    setup: tuple[Runtime, SeededServices, InvestigationWorkflow, InMemorySaver],
) -> None:
    runtime, _, workflow, _ = setup
    state = workflow.start(
        runtime.matter, tuple(runtime.approved), budgets=WorkflowBudgets(transitions=1)
    )
    assert state.stage == "BLOCKED" and state.errors == ("BUDGET_EXHAUSTED",)
    state = workflow.start(
        runtime.matter, tuple(runtime.approved), budgets=WorkflowBudgets(model_calls=0)
    )
    state = workflow.resume(linked(workflow, state))
    assert state.stage == "BLOCKED" and not state.finding_ids
    assert runtime.reasoner.calls == runtime.search.calls == 0


def test_zero_candidates_still_needs_second_review(
    setup: tuple[Runtime, SeededServices, InvestigationWorkflow, InMemorySaver],
) -> None:
    runtime, _, workflow, _ = setup
    state = workflow.start(runtime.matter, tuple(runtime.approved))
    state = workflow.resume(approval(state))  # No human entity links, no comparisons.
    assert state.review_gate == "findings" and not state.finding_ids
    assert runtime.reasoner.calls == runtime.search.calls == 0
    assert workflow.resume(approval(state)).stage == "APPROVED"


def test_safe_model_failure(
    setup: tuple[Runtime, SeededServices, InvestigationWorkflow, InMemorySaver],
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime, _, workflow, saver = setup

    def fail() -> None:
        raise RuntimeError("PRIVATE CREDENTIAL PROMPT")

    runtime.reasoner.hook = fail
    state = workflow.start(runtime.matter, tuple(runtime.approved))
    state = workflow.resume(linked(workflow, state))
    assert state.stage == "BLOCKED" and not state.finding_ids
    assert "PRIVATE" not in repr(list(saver.list(None))) + caplog.text


Output = TypeVar("Output", bound=BaseModel)


class FakeExtractor:
    provider = "offline"
    model = "synthetic"
    retries = 0

    def __init__(self, *, empty: bool = False, fail: bool = False) -> None:
        from lextrace.research.contracts import Usage

        self.usage = Usage()
        self.calls = 0
        self.empty = empty
        self.fail = fail

    def generate(
        self, prompt: object, schema: type[Output], context: dict[str, object]
    ) -> Output:
        self.calls += 1
        if self.fail:
            raise RuntimeError("PRIVATE EXTRACTION PROMPT")
        text = str(context["text"])
        return schema.model_validate(
            {
                "candidates": []
                if self.empty
                else [
                    {
                        "kind": "event",
                        "quote": text,
                    },
                    {
                        "kind": "assertion",
                        "speaker_quote": None,
                        "predicate_quote": None,
                        "subject_quote": None,
                        "object_quote": None,
                        "quote": text,
                    },
                ]
            }
        )


def accept_all(
    workflow: InvestigationWorkflow, state: InvestigationState
) -> HumanApproval:
    revisions = workflow.services.revisions(workflow.artifacts[state.thread_id])
    return approval(
        state,
        acknowledge_incomplete_coverage=True,
        evidence_decisions=tuple(
            command(
                r.original,
                expected_revision=r.revision,
                reviewer_id="human",
                decision_id="gate-" + str(index),
            )
            for index, r in enumerate(revisions)
        ),
    )


@pytest.mark.parametrize("coverage", ["COMPLETE", "PARTIAL", "OCR_REQUIRED"])
def test_real_m3_adapter_coverage_and_acceptance(
    setup: tuple[Runtime, SeededServices, InvestigationWorkflow, InMemorySaver],
    coverage: str,
) -> None:
    runtime, _, _, _ = setup
    version = runtime.repo.active_document_version(
        runtime.matter, runtime.approved[0].document_id
    )
    assert version
    version = runtime.repo.create_document_version(
        version.model_copy(
            update={"version_id": "m3-" + coverage, "coverage": coverage}
        )
    )
    extractor = FakeExtractor()
    services = InvestigationServices(
        runtime.store, runtime.repo, extractor, runtime.service
    )
    workflow = InvestigationWorkflow(services, checkpointer=InMemorySaver())
    first = workflow.start(
        runtime.matter,
        (
            ApprovedVersion(
                document_id=version.document_id, version_id=version.version_id
            ),
        ),
    )
    assert first.stage == "WAITING_REVIEW" and first.coverage == (coverage,)
    assert extractor.calls == (0 if coverage == "OCR_REQUIRED" else 1)
    if coverage != "COMPLETE":
        with pytest.raises(ValueError, match="coverage"):
            workflow.resume(approval(first))
    second = workflow.resume(accept_all(workflow, first))
    if coverage == "OCR_REQUIRED":
        assert second.stage == "BLOCKED" and second.errors == ("NO_EVIDENCE",)
    else:
        assert second.stage == "WAITING_REVIEW" and second.review_gate == "findings"
        chronology = workflow.artifacts[first.thread_id].reviewed_chronology
        assert chronology and len(chronology.unresolved_events) == 1
        assert (
            workflow.resume(
                approval(second, acknowledge_incomplete_coverage=True)
            ).stage
            == "APPROVED"
        )
        assert extractor.calls == 1


@pytest.mark.parametrize("failure", ["empty", "failure", "budget"])
def test_real_m3_empty_failed_or_budgeted(
    setup: tuple[Runtime, SeededServices, InvestigationWorkflow, InMemorySaver],
    failure: str,
) -> None:
    runtime, _, _, _ = setup
    extractor = FakeExtractor(empty=failure == "empty", fail=failure == "failure")
    services = InvestigationServices(
        runtime.store, runtime.repo, extractor, runtime.service
    )
    saver = InMemorySaver()
    workflow = InvestigationWorkflow(services, checkpointer=saver)
    state = workflow.start(
        runtime.matter,
        tuple(runtime.approved),
        budgets=WorkflowBudgets(extraction_windows=0 if failure == "budget" else 100),
    )
    assert state.stage == "WAITING_REVIEW"
    assert workflow.artifacts[state.thread_id].chronology is not None
    state = workflow.resume(approval(state, acknowledge_incomplete_coverage=True))
    assert state.stage == "BLOCKED" and state.errors == ("NO_EVIDENCE",)
    assert not state.finding_ids
    assert "PRIVATE EXTRACTION" not in repr(list(saver.list(None)))
    if failure == "budget":
        assert extractor.calls == 0


def test_cancel_during_investigation_prevents_second_gate(
    setup: tuple[Runtime, SeededServices, InvestigationWorkflow, InMemorySaver],
) -> None:
    runtime, services, workflow, _ = setup
    canceled = False
    services.stopped = lambda: canceled

    def cancel() -> None:
        nonlocal canceled
        canceled = True

    runtime.reasoner.hook = cancel
    first = workflow.start(runtime.matter, tuple(runtime.approved))
    final = workflow.resume(linked(workflow, first))
    assert final.stage == "CANCELED"
    assert workflow.pending_gate(first.thread_id) is None
    assert workflow.artifacts[first.thread_id].snapshot is None


def test_insufficient_evidence_is_still_proposed(
    setup: tuple[Runtime, SeededServices, InvestigationWorkflow, InMemorySaver],
) -> None:
    runtime, _, workflow, _ = setup
    from lextrace.investigations.findings import Hypothesis

    def insufficient(h: Hypothesis) -> Hypothesis:
        return h.model_copy(
            update={
                "premises": (
                    h.premises[0].model_copy(update={"entailment": "UNSUPPORTED"}),
                    *h.premises[1:],
                )
            }
        )

    runtime.verifier.mutate = insufficient
    state = workflow.start(runtime.matter, tuple(runtime.approved))
    state = workflow.resume(linked(workflow, state))
    assert state.review_gate == "findings" and state.verification == ("INSUFFICIENT",)
    assert workflow.resume(approval(state)).stage == "APPROVED"
    findings = workflow.artifacts[state.thread_id].result
    assert findings and findings.findings[0].status == "INSUFFICIENT_EVIDENCE"
    assert findings.findings[0].review_state == "PROPOSED"


def test_private_file_damage_before_resume(
    setup: tuple[Runtime, SeededServices, InvestigationWorkflow, InMemorySaver],
) -> None:
    runtime, _, workflow, _ = setup
    state = workflow.start(runtime.matter, tuple(runtime.approved))
    path = (
        runtime.store.private_root
        / runtime.matter
        / runtime.approved[0].document_id
        / "extracted.txt"
    )
    path.write_text("PRIVATE changed bytes")
    with pytest.raises(ValueError, match="Stale"):
        workflow.resume(approval(state))
    assert runtime.reasoner.calls == 0


def test_extraction_record_budget_before_review_writes(
    setup: tuple[Runtime, SeededServices, InvestigationWorkflow, InMemorySaver],
) -> None:
    runtime, _, _, _ = setup
    extractor = FakeExtractor()
    services = InvestigationServices(
        runtime.store, runtime.repo, extractor, runtime.service
    )
    workflow = InvestigationWorkflow(services, checkpointer=InMemorySaver())
    state = workflow.start(
        runtime.matter,
        tuple(runtime.approved),
        budgets=WorkflowBudgets(extraction_records=0),
    )
    assert state.stage == "BLOCKED" and state.errors == ("BUDGET_EXHAUSTED",)
    assert workflow.pending_gate(state.thread_id) is None
    assert runtime.reasoner.calls == 0


def test_corrected_date_requires_separate_accept(
    setup: tuple[Runtime, SeededServices, InvestigationWorkflow, InMemorySaver],
) -> None:
    from datetime import date

    from lextrace.investigations.contracts import InvestigationEvent
    from lextrace.investigations.review import DateCorrection

    runtime, _, _, _ = setup
    services = InvestigationServices(
        runtime.store, runtime.repo, FakeExtractor(), runtime.service
    )
    workflow = InvestigationWorkflow(services, checkpointer=InMemorySaver())
    first = workflow.start(runtime.matter, (runtime.approved[0],))
    request = accept_all(workflow, first)
    event = next(
        r
        for r in services.revisions(workflow.artifacts[first.thread_id])
        if isinstance(r.original, InvestigationEvent)
    )
    edit = command(
        event.original,
        expected_revision=1,
        decision_id="correct-date",
        reviewer_id="human",
        decision="EDIT",
        date_correction=DateCorrection(
            kind="exact",
            earliest=date(2020, 1, 1),
            latest=date(2020, 1, 1),
            precision="day",
        ),
    )
    accept = command(
        event.original,
        expected_revision=2,
        decision_id="accept-correction",
        reviewer_id="human",
    )
    decisions = tuple(
        c for c in request.evidence_decisions if c.target != edit.target
    ) + (edit, accept)
    second = workflow.resume(
        request.model_copy(update={"evidence_decisions": decisions})
    )
    assert second.review_gate == "findings"
    chronology = workflow.artifacts[first.thread_id].reviewed_chronology
    assert chronology and chronology.dated_groups[0].earliest == date(2020, 1, 1)
    assert isinstance(event.original, InvestigationEvent)
    assert event.original.temporal.earliest is None
    assert workflow.resume(approval(second)).stage == "APPROVED"

# ruff: noqa: F811 -- shared synthetic fixtures
"""M9 SQLite restart, stale source, ownership and deletion integration checks."""

import pytest

from lextrace.deposition.contracts import PlanError, ReviewPlan
from lextrace.deposition.service import DepositionService
from tests.deposition_fixtures import deposition as deposition  # noqa: F401
from tests.unit.test_deposition import create
from tests.unit.test_investigation_durable import Harness
from tests.unit.test_investigation_durable import durable as durable  # noqa: F401
from tests.unit.test_investigation_workflow import setup as memory_setup  # noqa: F401


def test_restart_recovers_exact_review_and_history(
    deposition: DepositionService, durable: Harness
) -> None:
    view = create(deposition, durable)
    b = view.basis
    request = ReviewPlan(
        decision_id="durable-review",
        expected_revision=1,
        action="APPROVE",
        questions=view.current.questions,
        confirm=True,
        acknowledge_limitations=True,
    )
    original = deposition.review(
        b.matter_id, b.thread_id, b.plan_id, request, "reviewer"
    )
    restarted = DepositionService(durable.restart())
    assert restarted.read(b.matter_id, b.thread_id, b.plan_id) == original
    assert (
        restarted.review(b.matter_id, b.thread_id, b.plan_id, request, "reviewer")
        == original
    )
    assert len(restarted.history(b.matter_id, b.thread_id, b.plan_id)) == 2
    assert (
        restarted.approved(b.matter_id, b.thread_id, b.plan_id, 2).current.status
        == "APPROVED"
    )


@pytest.mark.parametrize("change", ["version", "text", "review", "delete"])
def test_stale_dependencies_never_publish(
    deposition: DepositionService, durable: Harness, change: str
) -> None:
    from tests.unit.test_investigation_review import command

    view = create(deposition, durable)
    b = view.basis
    ref = b.evidence[0].reference
    if change == "version":
        old = durable.runtime.repo.active_document_version(b.matter_id, ref.document_id)
        assert old is not None
        durable.runtime.repo.create_document_version(
            old.model_copy(update={"version_id": "new-version"})
        )
    elif change == "text":
        (
            durable.root / "private" / b.matter_id / ref.document_id / "extracted.txt"
        ).write_text("changed")
    elif change == "review":
        revision = b.witnesses[0].accounts[0]
        durable.runtime.repo.review(
            command(
                revision.original,
                expected_revision=revision.revision,
                decision_id="later-review",
                decision="REJECT",
            )
        )
    else:
        durable.runtime.store.delete_document(b.matter_id, ref.document_id)
        for table in (
            "deposition_plans",
            "deposition_revisions",
            "deposition_decisions",
        ):
            assert deposition.runtime.read(f"SELECT COUNT(*) FROM {table}")[0][0] == 0
    restarted = DepositionService(durable.restart())
    with pytest.raises(PlanError):
        restarted.read(b.matter_id, b.thread_id, b.plan_id)
    request = ReviewPlan(
        decision_id="late",
        expected_revision=1,
        action="APPROVE",
        questions=view.current.questions,
        confirm=True,
        acknowledge_limitations=True,
    )
    with pytest.raises(PlanError):
        restarted.review(b.matter_id, b.thread_id, b.plan_id, request, "reviewer")


def test_concurrent_review_and_revision_invalidate_current_approval(
    deposition: DepositionService, durable: Harness
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    view = create(deposition, durable)
    b = view.basis

    def decide(identity: str) -> str:
        request = ReviewPlan(
            decision_id=identity,
            expected_revision=1,
            action="APPROVE",
            questions=view.current.questions,
            confirm=True,
            acknowledge_limitations=True,
        )
        try:
            return deposition.review(
                b.matter_id, b.thread_id, b.plan_id, request, "human"
            ).current.status
        except PlanError:
            return "CONFLICT"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(decide, ("one", "two")))
    assert sorted(results) == ["APPROVED", "CONFLICT"]
    current = deposition.read(b.matter_id, b.thread_id, b.plan_id)
    revision = ReviewPlan(
        decision_id="revise",
        expected_revision=2,
        action="REVISE",
        questions=current.current.questions,
    )
    revised = deposition.review(b.matter_id, b.thread_id, b.plan_id, revision, "human")
    assert revised.current.status == "DRAFT"
    assert (
        deposition.approved(b.matter_id, b.thread_id, b.plan_id, 2).current.status
        == "APPROVED"
    )
    with pytest.raises(PlanError):
        deposition.approved(b.matter_id, b.thread_id, b.plan_id, 3)


def test_review_failure_rolls_back_revision_and_idempotency_journal(
    deposition: DepositionService, durable: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    from lextrace.deposition.contracts import PlanRevision
    from lextrace.deposition.storage import PlanStorage

    view = create(deposition, durable)
    b = view.basis
    request = ReviewPlan(
        decision_id="recover-review",
        expected_revision=1,
        action="APPROVE",
        questions=view.current.questions,
        confirm=True,
        acknowledge_limitations=True,
    )
    append = PlanStorage.append

    def failed(db: sqlite3.Connection, revision: PlanRevision) -> None:
        append(db, revision)
        raise RuntimeError("Simulated process failure before transaction commit")

    with monkeypatch.context() as patch:
        patch.setattr(PlanStorage, "append", failed)
        with pytest.raises(RuntimeError):
            deposition.review(b.matter_id, b.thread_id, b.plan_id, request, "human")
    assert deposition.read(b.matter_id, b.thread_id, b.plan_id).current.revision == 1
    assert (
        deposition.runtime.read("SELECT COUNT(*) FROM deposition_decisions")[0][0] == 0
    )
    restarted = DepositionService(durable.restart())
    assert (
        restarted.review(
            b.matter_id, b.thread_id, b.plan_id, request, "human"
        ).current.revision
        == 2
    )

# ruff: noqa: F811 -- shared synthetic durable fixture
"""Request identity shares the M7 run transaction and recovery fence."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from lextrace.investigations.workflow import InvestigationState, InvestigationWorkflow
from tests.unit.test_investigation_durable import Crash, Harness, durable  # noqa: F401
from tests.unit.test_investigation_workflow import setup as memory_setup  # noqa: F401


def start(
    workflow: InvestigationWorkflow,
    durable: Harness,
    *,
    operator: str = "TEST-ONLY-operator",
) -> InvestigationState:
    return workflow.start(
        durable.runtime.matter,
        tuple(durable.runtime.approved),
        request_id="retry-identity",
        operator_id=operator,
    )


@pytest.mark.parametrize(
    "boundary",
    [
        "before_start_commit",
        "after_start_commit",
        "after_artifact_commit:extract_verify",
    ],
)
def test_crash_and_restart_keep_one_request_and_extraction(
    durable: Harness, boundary: str
) -> None:
    durable.inject(boundary)
    with pytest.raises(Crash):
        start(durable.workflow, durable)
    workflow = durable.restart()
    resumed = start(workflow, durable)
    assert resumed.review_gate == "evidence"
    assert start(workflow, durable) == resumed
    assert durable.extraction_calls == 1
    assert len(durable.runtime.repo.list_threads(durable.runtime.matter)) == 1
    assert len(workflow.runtime.read("SELECT * FROM investigation_start_requests")) == 1  # type: ignore[union-attr]


def test_wrong_operator_and_tampered_request_owner_fail_closed(
    durable: Harness,
) -> None:
    first = start(durable.workflow, durable)
    with pytest.raises(ValueError, match="conflict"):
        start(durable.workflow, durable, operator="another-operator")
    with durable.runtime.repo._db:
        durable.runtime.repo._db.execute(
            "UPDATE investigation_start_requests SET initial_state=?",
            (first.model_copy(update={"matter_id": "foreign"}).model_dump_json(),),
        )
    with pytest.raises(ValueError, match="owner"):
        start(durable.workflow, durable)
    assert durable.extraction_calls == 1


def test_peer_while_inflight_conflicts_then_recovers_same_run(
    durable: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = durable.workflow
    peer = durable.peer()
    entered, release = Event(), Event()
    original = first.services.extract

    def extract(state: InvestigationState) -> object:
        entered.set()
        assert release.wait(5)
        return original(state)

    monkeypatch.setattr(first.services, "extract", extract)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(start, first, durable)
        try:
            assert entered.wait(5)
            with pytest.raises(ValueError, match="busy"):
                start(peer, durable)
        finally:
            release.set()
        state = future.result(timeout=10)
    assert start(peer, durable) == state
    assert durable.extraction_calls == 1


def test_source_deletion_keeps_request_tombstone_without_source_state(
    durable: Harness,
) -> None:
    state = start(durable.workflow, durable)
    durable.runtime.store.delete_document(
        durable.runtime.matter, durable.runtime.approved[0].document_id
    )
    assert durable.workflow.runtime is not None
    rows = durable.workflow.runtime.read(
        "SELECT thread_id,initial_state FROM investigation_start_requests"
    )
    assert rows == [(None, None)]
    with pytest.raises(ValueError):
        start(durable.workflow, durable)
    assert state.thread_id not in durable.runtime.repo.list_threads(
        durable.runtime.matter
    )


def test_missing_executed_checkpoint_never_restarts_extraction(
    durable: Harness,
) -> None:
    state = start(durable.workflow, durable)
    assert durable.workflow.runtime is not None
    durable.workflow.runtime.saver.delete_thread(state.thread_id)
    with pytest.raises(ValueError, match="checkpoint"):
        start(durable.workflow, durable)
    assert durable.extraction_calls == 1
    assert len(durable.runtime.repo.list_threads(durable.runtime.matter)) == 1

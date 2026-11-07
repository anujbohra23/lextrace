"""M7B recovery uses real SQLite checkpoints and private domain transactions."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Event
from typing import Any, cast

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from lextrace.investigations.evidence_search import EvidenceSearchService
from lextrace.investigations.inconsistency_investigation import (
    InconsistencyInvestigationService,
)
from lextrace.investigations.repository import InvestigationRepository
from lextrace.investigations.workflow import (
    HumanApproval,
    InvestigationState,
    InvestigationWorkflow,
)
from lextrace.matter.store import MatterStore
from tests.unit.test_inconsistency_investigation import Runtime
from tests.unit.test_investigation_review import command
from tests.unit.test_investigation_workflow import (
    SeededServices,
    approval,
    linked,
)
from tests.unit.test_investigation_workflow import (
    setup as memory_setup,  # noqa: F401, F811 -- shared synthetic fixture
)

MemorySetup = tuple[Runtime, SeededServices, InvestigationWorkflow, InMemorySaver]


class Crash(BaseException):
    """Emulate process loss without the workflow's safe Exception boundary."""


@dataclass
class Harness:
    runtime: Runtime
    services: SeededServices
    workflow: InvestigationWorkflow
    root: Path
    opened: list[tuple[InvestigationWorkflow, InvestigationRepository, MatterStore]] = (
        field(default_factory=list)
    )

    def restart(self) -> InvestigationWorkflow:
        self.workflow.close()
        return self.peer()

    def peer(self) -> InvestigationWorkflow:
        store = MatterStore(self.root / "db", self.root / "private")
        repo = InvestigationRepository(self.root / "db", source_store=store)
        self.runtime.search.inner = EvidenceSearchService(store, repo)
        services = replace(
            self.services,
            store=store,
            repository=repo,
            investigator=InconsistencyInvestigationService(
                store,
                repo,
                self.runtime.search,
                self.runtime.reasoner,
                self.runtime.verifier,
            ),
        )
        self.workflow = InvestigationWorkflow.open(services)
        self.opened.append((self.workflow, repo, store))
        return self.workflow

    @property
    def extraction_calls(self) -> int:
        return self.services.extraction_calls + sum(
            cast(SeededServices, w.services).extraction_calls for w, _, _ in self.opened
        )

    def start(self) -> InvestigationState:
        return self.workflow.start(self.runtime.matter, tuple(self.runtime.approved))

    def inject(self, boundary: str) -> None:
        assert self.workflow.runtime is not None

        def crash(point: str) -> None:
            if point == boundary:
                raise Crash(point)

        self.workflow.runtime.crash = crash


@pytest.fixture
def durable(  # noqa: F811 -- imported pytest fixture
    memory_setup: MemorySetup,  # noqa: F811 -- imported pytest fixture
    tmp_path: Path,
) -> Iterator[Harness]:
    runtime, services, _, _ = memory_setup
    workflow = InvestigationWorkflow.open(services)
    harness = Harness(runtime, services, workflow, tmp_path)
    yield harness
    workflow.close()
    for opened, repo, store in harness.opened:
        opened.close()
        repo.close()
        store.close()


def test_restart_both_gates(durable: Harness, caplog: pytest.LogCaptureFixture) -> None:
    first = durable.start()
    request = linked(durable.workflow, first)
    workflow = durable.restart()
    assert workflow.reopen(first.thread_id) == first
    second = workflow.resume(request)
    assert second.review_gate == "findings"
    workflow = durable.restart()
    assert workflow.reopen(first.thread_id) == second
    final = workflow.resume(approval(second))
    assert final.stage == "APPROVED"
    assert durable.extraction_calls == durable.runtime.reasoner.calls == 1
    assert workflow.approved_snapshot(final.thread_id).meaning == (
        "HUMAN_SIGNED_INVESTIGATION_HYPOTHESES"
    )
    assert workflow.runtime is not None
    payload = repr(list(workflow.runtime.saver.list(None))) + caplog.text
    for secret in (
        "I delivered",
        "I did not deliver",
        "normalized_proposition",
        "exact_quote",
    ):
        assert secret not in payload


def decision_request(harness: Harness, state: InvestigationState) -> HumanApproval:
    revisions = harness.services.revisions(harness.workflow.artifacts[state.thread_id])
    return linked(harness.workflow, state).model_copy(
        update={
            "evidence_decisions": tuple(
                command(
                    r.original,
                    expected_revision=r.revision,
                    decision_id=f"gate-{i}",
                    reviewer_id="human",
                    explanation="PRIVATE reviewer explanation",
                )
                for i, r in enumerate(revisions[:2])
            )
        }
    )


@pytest.mark.parametrize(
    "boundary",
    [
        "before_intent",
        "after_intent",
        "before_decisions",
        "between_decisions",
        "before_decision_commit",
        "after_decision_commit",
        "after_graph_continuation",
    ],
)
def test_approval_crash_reconciliation(durable: Harness, boundary: str) -> None:
    first = durable.start()
    request = decision_request(durable, first)
    original = [
        durable.runtime.repo.review_history(d.target)
        for d in request.evidence_decisions
    ]
    durable.inject(boundary)
    with pytest.raises(Crash):
        durable.workflow.resume(request)
    if boundary in (
        "before_intent",
        "after_intent",
        "before_decisions",
        "between_decisions",
        "before_decision_commit",
    ):
        assert [
            durable.runtime.repo.review_history(d.target)
            for d in request.evidence_decisions
        ] == original
    workflow = durable.restart()
    second = workflow.resume(request)
    assert second.review_gate == "findings"
    for decision, history in zip(request.evidence_decisions, original, strict=True):
        assert (
            len(durable.runtime.repo.review_history(decision.target))
            == len(history) + 1
        )
    assert workflow.resume(request) == second
    workflow = durable.restart()
    assert workflow.resume(request) == second
    assert durable.runtime.reasoner.calls == 1
    assert workflow.runtime is not None
    assert "PRIVATE reviewer explanation" not in repr(
        list(workflow.runtime.saver.list(None))
    )


@pytest.mark.parametrize(
    "boundary",
    [
        "before_publication_commit",
        "after_publication_commit",
        "after_graph_continuation",
    ],
)
def test_publication_crash_no_duplicates(durable: Harness, boundary: str) -> None:
    first = durable.start()
    second = durable.workflow.resume(linked(durable.workflow, first))
    request = approval(second)
    durable.inject(boundary)
    with pytest.raises(Crash):
        durable.workflow.resume(request)
    workflow = durable.restart()
    assert workflow.reopen(first.thread_id).stage == "APPROVED"
    snapshot = workflow.approved_snapshot(first.thread_id)
    assert workflow.resume(request).stage == "APPROVED"
    assert workflow.approved_snapshot(first.thread_id) == snapshot
    assert (
        durable.runtime.repo._db.execute(
            "SELECT COUNT(*) FROM investigation_snapshots"
        ).fetchone()[0]
        == 1
    )
    assert durable.runtime.reasoner.calls == 1


def test_reopen_reconciles_intent(durable: Harness) -> None:
    first = durable.start()
    request = decision_request(durable, first)
    durable.inject("after_decision_commit")
    with pytest.raises(Crash):
        durable.workflow.resume(request)
    workflow = durable.restart()
    assert workflow.reopen(first.thread_id).review_gate == "findings"


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
def test_wrong_approval_after_restart(
    durable: Harness, field: str, value: str, gate: str
) -> None:
    first = durable.start()
    if gate == "findings":
        first = durable.workflow.resume(linked(durable.workflow, first))
        if field == "gate":
            value = "evidence"
    workflow = durable.restart()
    with pytest.raises(ValueError):
        workflow.resume(approval(first, **{field: value}))
    assert workflow.pending_gate(first.thread_id) == first
    resumed = workflow.resume(approval(first))
    assert (
        resumed.review_gate == "findings"
        if gate == "evidence"
        else resumed.stage == "APPROVED"
    )


def test_concurrent_conflicting_approval(durable: Harness) -> None:
    first = durable.start()
    workflow = durable.workflow
    peer = durable.peer()
    entered, release = Event(), Event()
    assert workflow.runtime is not None

    def pause(boundary: str) -> None:
        if boundary == "after_intent":
            entered.set()
            assert release.wait(5)

    workflow.runtime.crash = pause
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(workflow.resume, approval(first))
        assert entered.wait(5)
        try:
            with pytest.raises(ValueError, match="busy"):
                peer.resume(approval(first, decision="REJECT"))
        finally:
            release.set()
        result = future.result()
    with pytest.raises(ValueError, match="Conflicting"):
        peer.resume(approval(first, decision="REJECT"))
    assert peer.resume(approval(first)) == result
    assert (
        durable.runtime.repo._db.execute(
            "SELECT COUNT(*) FROM investigation_intents"
        ).fetchone()[0]
        == 1
    )


def test_cancel_survives_restart(durable: Harness) -> None:
    first = durable.start()
    durable.workflow.cancel(first.thread_id)
    workflow = durable.restart()
    assert workflow.reopen(first.thread_id).stage == "CANCELED"
    with pytest.raises(ValueError):
        workflow.resume(approval(first))
    with pytest.raises(ValueError):
        workflow.approved_snapshot(first.thread_id)


@pytest.mark.parametrize("change", ["version", "delete", "text", "bytes", "review"])
@pytest.mark.parametrize("gate", ["evidence", "findings", "approved"])
def test_downtime_stale_dependencies(durable: Harness, change: str, gate: str) -> None:
    state = durable.start()
    if gate != "evidence":
        state = durable.workflow.resume(linked(durable.workflow, state))
    if gate == "approved":
        state = durable.workflow.resume(approval(state))
    document = state.sources[0].document_id
    if change == "version":
        version = durable.runtime.repo.active_document_version(
            state.matter_id, document
        )
        assert version is not None
        durable.runtime.repo.create_document_version(
            version.model_copy(update={"version_id": "replacement"})
        )
    elif change == "delete":
        durable.runtime.store.delete_document(state.matter_id, document)
        assert (
            durable.runtime.repo._db.execute(
                "SELECT COUNT(*) FROM investigation_artifacts"
            ).fetchone()[0]
            == 0
        )
    elif change in ("text", "bytes"):
        name = "extracted.txt" if change == "text" else "source.txt"
        (durable.root / "private" / state.matter_id / document / name).write_text(
            "changed"
        )
    else:
        revision = durable.services.revisions(
            durable.workflow.artifacts[state.thread_id]
        )[0]
        durable.runtime.repo.review(
            command(
                revision.original,
                expected_revision=revision.revision,
                decision_id="downtime",
                decision="REJECT",
            )
        )
    workflow = durable.restart()
    with pytest.raises(ValueError):
        workflow.reopen(state.thread_id)
    if gate == "approved":
        with pytest.raises(ValueError):
            workflow.approved_snapshot(state.thread_id)
    if change == "delete":
        assert workflow.runtime is not None
        assert list(workflow.runtime.saver.list(None)) == []


@pytest.mark.parametrize("damage", ["missing", "corrupt", "ownership", "schema"])
def test_artifacts_fail_closed(durable: Harness, damage: str) -> None:
    state = durable.start()
    db = durable.runtime.repo._db
    with db:
        if damage == "missing":
            db.execute(
                "DELETE FROM investigation_artifacts WHERE thread_id=?",
                (state.thread_id,),
            )
        elif damage == "corrupt":
            db.execute(
                "UPDATE investigation_artifacts SET payload=x'00' WHERE thread_id=?",
                (state.thread_id,),
            )
        else:
            import json
            from hashlib import sha256

            rows = db.execute(
                "SELECT node,payload FROM investigation_artifacts"
            ).fetchall()
            for node, raw in rows:
                envelope = json.loads(raw)
                envelope["matter_id" if damage == "ownership" else "schema"] = "wrong"
                payload = json.dumps(envelope).encode()
                db.execute(
                    "UPDATE investigation_artifacts SET payload=?,digest=? WHERE "
                    "node=?",
                    (payload, sha256(payload).hexdigest(), node),
                )
    workflow = durable.restart()
    with pytest.raises(ValueError, match="artifact"):
        workflow.reopen(state.thread_id)
    assert durable.runtime.reasoner.calls == 0


@pytest.mark.parametrize("coverage", ["PARTIAL", "OCR_REQUIRED"])
def test_incomplete_coverage_survives_restart(durable: Harness, coverage: Any) -> None:
    durable.services.verified = tuple(
        replace(r, coverage=replace(r.coverage, status=coverage))
        for r in durable.services.verified
    )
    first = durable.start()
    workflow = durable.restart()
    assert workflow.reopen(first.thread_id).coverage == (coverage,)
    with pytest.raises(ValueError, match="coverage"):
        workflow.resume(approval(first))
    second = workflow.resume(approval(first, acknowledge_incomplete_coverage=True))
    assert second.coverage == (coverage,)
    with pytest.raises(ValueError, match="coverage"):
        workflow.resume(approval(second))


@pytest.mark.parametrize(
    "node",
    [
        "extract_verify",
        "prepare_evidence_gate",
        "review_evidence",
        "load_accepted_reviews",
        "select_candidates",
        "investigate_context",
        "prepare_findings_gate",
        "review_findings",
        "finalize_snapshot",
    ],
)
def test_artifact_checkpoint_gap(durable: Harness, node: str) -> None:
    if node in ("extract_verify", "prepare_evidence_gate"):
        durable.inject(f"after_artifact_commit:{node}")
        with pytest.raises(Crash):
            durable.start()
        thread = durable.runtime.repo._db.execute(
            "SELECT thread_id FROM investigation_runs"
        ).fetchone()[0]
    else:
        first = durable.start()
        thread = first.thread_id
        if node in ("review_findings", "finalize_snapshot"):
            state = durable.workflow.resume(linked(durable.workflow, first))
            request = approval(state)
        else:
            request = linked(durable.workflow, first)
        durable.inject(f"after_artifact_commit:{node}")
        with pytest.raises(Crash):
            durable.workflow.resume(request)
    workflow = durable.restart()
    recovered = workflow.reopen(thread)
    if recovered.review_gate == "evidence":
        recovered = workflow.resume(linked(workflow, recovered))
    if recovered.review_gate == "findings":
        recovered = workflow.resume(approval(recovered))
    assert recovered.stage == "APPROVED"
    assert durable.extraction_calls == 1
    assert durable.runtime.reasoner.calls == 1
    assert (
        durable.runtime.repo._db.execute(
            "SELECT COUNT(*) FROM investigation_snapshots"
        ).fetchone()[0]
        == 1
    )


@pytest.mark.parametrize("node", ["extract_verify", "investigate_context"])
def test_unknown_tool_outcome_is_not_repeated(durable: Harness, node: str) -> None:
    if node == "extract_verify":
        durable.inject(f"before_artifact_commit:{node}")
        with pytest.raises(Crash):
            durable.start()
        thread = durable.runtime.repo._db.execute(
            "SELECT thread_id FROM investigation_runs"
        ).fetchone()[0]
    else:
        first = durable.start()
        thread = first.thread_id
        durable.inject(f"before_artifact_commit:{node}")
        with pytest.raises(Crash):
            durable.workflow.resume(linked(durable.workflow, first))
    calls = durable.runtime.reasoner.calls
    workflow = durable.restart()
    with pytest.raises(ValueError, match="Interrupted tool"):
        workflow.reopen(thread)
    assert durable.extraction_calls == 1
    assert durable.runtime.reasoner.calls == calls


def test_domain_change_at_publication_boundary(durable: Harness) -> None:
    first = durable.start()
    second = durable.workflow.resume(linked(durable.workflow, first))
    runtime = durable.workflow.runtime
    assert runtime is not None
    publish = runtime.publish

    def changed(state: InvestigationState, artifacts: Any) -> None:
        dependency = state.dependencies[0]
        current = durable.runtime.repo.current_review(dependency.target)
        assert current is not None
        durable.runtime.repo.review(
            command(
                current.original,
                expected_revision=current.revision,
                decision_id="publication-race",
                decision="REJECT",
            )
        )
        publish(state, artifacts)

    runtime.publish = changed  # type: ignore[method-assign]
    assert durable.workflow.resume(approval(second)).stage == "BLOCKED"
    assert (
        durable.runtime.repo._db.execute(
            "SELECT COUNT(*) FROM investigation_snapshots"
        ).fetchone()[0]
        == 0
    )


def test_publication_excludes_competing_review_writer(durable: Harness) -> None:
    first = durable.start()
    second = durable.workflow.resume(linked(durable.workflow, first))
    workflow = durable.workflow
    peer = durable.peer()
    assert peer.runtime is not None and workflow.runtime is not None
    dependency = second.dependencies[0]
    current = peer.services.repository.current_review(dependency.target)
    assert current is not None
    entered, release, writer_started, written = Event(), Event(), Event(), Event()

    def pause(boundary: str) -> None:
        if boundary == "before_publication_commit":
            entered.set()
            assert release.wait(5)

    def write() -> None:
        writer_started.set()
        peer.services.repository.review(
            command(
                current.original,
                expected_revision=current.revision,
                decision_id="after-publish",
                decision="REJECT",
            )
        )
        written.set()

    workflow.runtime.crash = pause
    with ThreadPoolExecutor(max_workers=2) as pool:
        publication = pool.submit(workflow.resume, approval(second))
        assert entered.wait(5)
        writer = pool.submit(write)
        assert writer_started.wait(5)
        assert not written.wait(0.05)
        release.set()
        assert publication.result().stage == "APPROVED"
        writer.result()
    with pytest.raises(ValueError, match="Stale"):
        workflow.approved_snapshot(first.thread_id)


def test_matter_deletion_cascades_private_workflow(durable: Harness) -> None:
    state = durable.start()
    durable.runtime.store.delete_matter(state.matter_id)
    for table in (
        "investigation_runs",
        "investigation_artifacts",
        "investigation_intents",
        "investigation_snapshots",
    ):
        assert (
            durable.runtime.repo._db.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            == 0
        )
    workflow = durable.restart()
    with pytest.raises(ValueError):
        workflow.reopen(state.thread_id)
    assert workflow.runtime is not None
    assert list(workflow.runtime.saver.list(None)) == []


def test_cleanup_on_existing_workflow(durable: Harness) -> None:
    first = durable.start()
    durable.runtime.store.delete_document(first.matter_id, first.sources[0].document_id)
    assert durable.workflow.recover_cleanup() == 1
    assert first.thread_id not in durable.workflow.artifacts
    assert durable.workflow.recover_cleanup() == 0
    assert durable.workflow.runtime is not None
    assert list(durable.workflow.runtime.saver.list(None)) == []


def test_cancel_after_decision_commit(durable: Harness) -> None:
    first = durable.start()
    durable.inject("after_decision_commit")
    with pytest.raises(Crash):
        durable.workflow.resume(decision_request(durable, first))
    durable.workflow.cancel(first.thread_id)
    workflow = durable.restart()
    assert workflow.reopen(first.thread_id).cancelled
    with pytest.raises(ValueError):
        workflow.resume(approval(first))
    assert (
        durable.runtime.repo._db.execute(
            "SELECT COUNT(*) FROM investigation_snapshots"
        ).fetchone()[0]
        == 0
    )


def test_checkpoint_schema_requires_explicit_upgrade(durable: Harness) -> None:
    runtime = durable.workflow.runtime
    assert runtime is not None
    with runtime.connection:
        runtime.connection.execute("UPDATE lextrace_checkpoint_schema SET version=99")
    durable.workflow.close()
    with pytest.raises(ValueError, match="explicit migration"):
        InvestigationWorkflow.open(durable.services)


def test_corrupt_cached_transition_is_rejected(durable: Harness) -> None:
    first = durable.start()
    with durable.runtime.repo._db as db:
        db.execute(
            "UPDATE investigation_artifacts SET result=? WHERE thread_id=? "
            "AND node='prepare_evidence_gate'",
            (
                '{"stage":"APPROVED","private":"PRIVATE DAMAGED ARTIFACT"}',
                first.thread_id,
            ),
        )
    workflow = durable.restart()
    with pytest.raises(ValueError) as error:
        workflow.reopen(first.thread_id)
    assert "PRIVATE DAMAGED ARTIFACT" not in str(error.value)


def test_runtime_files_are_private(durable: Harness) -> None:
    durable.start()
    directory = durable.root / "private" / ".investigations-v1"
    assert directory.stat().st_mode & 0o777 == 0o700
    for name in ("checkpoints.sqlite", "workflow.lock"):
        assert (directory / name).stat().st_mode & 0o777 == 0o600


def test_process_exit_releases_runtime_claim(durable: Harness) -> None:
    import subprocess
    import sys

    first = durable.start()
    directory = durable.root / "private" / ".investigations-v1"
    script = (
        "import fcntl, os, sys; "
        "fd = os.open(sys.argv[1], os.O_RDWR); "
        "fcntl.flock(fd, fcntl.LOCK_EX); "
        "print('claimed', flush=True); sys.stdin.read(1); os._exit(9)"
    )
    with subprocess.Popen(
        [sys.executable, "-c", script, str(directory / "workflow.lock")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    ) as process:
        assert process.stdout is not None
        try:
            assert process.stdout.readline().strip() == "claimed"
            with pytest.raises(ValueError, match="busy"):
                durable.workflow.resume(approval(first))
        finally:
            process.communicate("x", timeout=5)
        assert process.returncode == 9
    assert durable.workflow.resume(approval(first)).review_gate == "findings"

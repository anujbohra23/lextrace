"""Private M7B journal. Domain transactions and checkpoint commits are separate.

The OS advisory lock fences all local workflow instances, including separate
processes, until execution ends or the process dies. SQLite transactions fence
source/review writers. No time-based lease can expire under a live tool call.
"""

import fcntl
import json
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Any

from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import TypeAdapter

from lextrace.investigations.contracts import DocumentVersion, EvidenceReference
from lextrace.investigations.provenance import validate_evidence_reference
from lextrace.investigations.review import ProjectionDependency
from lextrace.matter.contracts import DocumentSection

if TYPE_CHECKING:
    from lextrace.investigations.workflow import (
        HumanApproval,
        InvestigationArtifacts,
        InvestigationServices,
        InvestigationState,
    )

MAX_ARTIFACT_BYTES = 32 * 1024 * 1024


class DurableRuntime:
    """Caller-owned saver connection; close after the workflow is idle."""

    def __init__(self, services: "InvestigationServices") -> None:
        self.services = services
        self.repo = services.repository
        database = self.repo._db.execute("PRAGMA database_list").fetchone()[2]
        source_database = services.store._db.execute("PRAGMA database_list").fetchone()[
            2
        ]
        if Path(database).resolve() != Path(source_database).resolve():
            raise ValueError("Workflow services must share the matter database.")
        self._local = RLock()
        self._fd: int | None = None
        self.crash: Callable[[str], None] = lambda boundary: None
        root = services.store.private_root
        if root.is_symlink() or not os.path.samestat(
            root.stat(), services.store._root_identity
        ):
            raise ValueError("Private runtime root changed.")
        directory = root / ".investigations-v1"
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink():
            raise ValueError("Unsafe private runtime directory.")
        directory.chmod(0o700)
        self._lock_path = directory / "workflow.lock"
        database = directory / "checkpoints.sqlite"
        self._secure_file(database)
        self.connection = sqlite3.connect(database, check_same_thread=False)
        # Explicit wrapper schema policy, in addition to the saver-owned schema.
        try:
            with self.connection:
                self.connection.execute(
                    "CREATE TABLE IF NOT EXISTS lextrace_checkpoint_schema "
                    "(version INTEGER PRIMARY KEY, saver TEXT NOT NULL)"
                )
                rows = self.connection.execute(
                    "SELECT version,saver FROM lextrace_checkpoint_schema"
                ).fetchall()
                if rows and rows != [(1, "sqlite-3.1.3")]:
                    raise ValueError(
                        "Unsupported checkpoint schema; explicit migration required."
                    )
                if (
                    not rows
                    and self.connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='checkpoints'"
                    ).fetchone()
                ):
                    raise ValueError(
                        "Unversioned checkpoint database requires migration."
                    )
                self.connection.execute(
                    "INSERT OR IGNORE INTO lextrace_checkpoint_schema VALUES "
                    "(1,'sqlite-3.1.3')"
                )
        except BaseException:
            self.connection.close()
            raise
        self.saver = SqliteSaver(self.connection)

    @staticmethod
    def _secure_file(path: Path) -> None:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)

    def __enter__(self) -> "DurableRuntime":
        self._local.acquire()
        try:
            if self._fd is not None:
                raise ValueError("Investigation runtime is busy; retry.")
            self._secure_file(self._lock_path)
            fd = os.open(self._lock_path, os.O_RDWR | os.O_NOFOLLOW)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BaseException as error:
                os.close(fd)
                if isinstance(error, BlockingIOError):
                    raise ValueError("Investigation runtime is busy; retry.") from None
                raise
            self._fd = fd
            return self
        except BaseException:
            self._local.release()
            raise

    def __exit__(self, *args: object) -> None:
        assert self._fd is not None
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None
            self._local.release()

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.repo._lock:
            db = self.repo._db
            try:
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def create(
        self, state: "InvestigationState", artifacts: "InvestigationArtifacts"
    ) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO investigation_runs(thread_id,matter_id,run_id) "
                "VALUES (?,?,?)",
                (state.thread_id, state.matter_id, state.run_id),
            )
            for source in state.sources:
                db.execute(
                    "INSERT INTO investigation_run_sources VALUES (?,?)",
                    (state.thread_id, source.document_id),
                )
            self._write(db, state.thread_id, "initial", artifacts, {})

    def read(self, query: str, args: tuple[object, ...] = ()) -> list[Any]:
        with self.repo._lock:
            return self.repo._db.execute(query, args).fetchall()

    def require_live(self, state: "InvestigationState") -> None:
        rows = self.read(
            "SELECT matter_id,run_id,status FROM investigation_runs WHERE thread_id=?",
            (state.thread_id,),
        )
        row = rows[0] if rows else None
        if row != (state.matter_id, state.run_id, "LIVE"):
            raise ValueError("Investigation canceled, deleted or unavailable.")

    def _adapter(self) -> TypeAdapter[Any]:
        from lextrace.investigations.workflow import InvestigationArtifacts

        return TypeAdapter(InvestigationArtifacts)

    def _write(
        self,
        db: sqlite3.Connection,
        thread: str,
        node: str,
        artifacts: "InvestigationArtifacts",
        result: dict[str, object] | None,
    ) -> None:
        owner = db.execute(
            "SELECT matter_id,run_id FROM investigation_runs WHERE thread_id=?",
            (thread,),
        ).fetchone()
        if owner is None:
            raise ValueError("Private artifact owner unavailable.")
        try:
            normalized_result = (
                json.loads(
                    TypeAdapter(dict[str, object]).dump_json(
                        {
                            key: value
                            for key, value in result.items()
                            if key != "artifact_digest"
                        }
                    )
                )
                if result is not None
                else None
            )
            payload = json.dumps(
                {
                    "schema": 1,
                    "node": node,
                    "result": normalized_result,
                    "matter_id": owner[0],
                    "run_id": owner[1],
                    "thread_id": thread,
                    "artifacts": json.loads(
                        self._adapter().dump_json(artifacts, warnings="error")
                    ),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        except Exception:
            raise ValueError("Private artifact serialization failed.") from None
        if len(payload) > MAX_ARTIFACT_BYTES:
            raise ValueError("Private artifact budget exceeded.")
        digest = sha256(payload).hexdigest()
        if result is not None:
            result["artifact_digest"] = digest
        db.execute(
            "INSERT INTO investigation_artifacts VALUES (?,?,?,?,?)",
            (
                thread,
                node,
                digest,
                payload,
                TypeAdapter(dict[str, object]).dump_json(result).decode()
                if result is not None
                else None,
            ),
        )

    def load(
        self, thread: str, node: str | None = None, *, digest: str | None = None
    ) -> tuple["InvestigationArtifacts", dict[str, object] | None]:
        """Load a hash-verified journal entry, optionally pinned to a signed digest."""
        query = (
            "SELECT digest,payload,result,node FROM investigation_artifacts "
            "WHERE thread_id=?"
        )
        args: tuple[str, ...] = (thread,)
        if node is not None:
            query += " AND node=?"
            args += (node,)
        if digest is not None:
            query += " AND digest=?"
            args += (digest,)
        query += " ORDER BY rowid DESC LIMIT 1"
        rows = self.read(query, args)
        row = rows[0] if rows else None
        if row is None:
            raise ValueError("Private artifacts unavailable.")
        try:
            if len(row[1]) > MAX_ARTIFACT_BYTES or sha256(row[1]).hexdigest() != row[0]:
                raise ValueError
            envelope = json.loads(row[1])
            owners = self.read(
                "SELECT matter_id,run_id FROM investigation_runs WHERE thread_id=?",
                (thread,),
            )
            owner = owners[0] if owners else None
            if owner is None or (
                envelope["schema"],
                envelope["matter_id"],
                envelope["run_id"],
                envelope["thread_id"],
            ) != (1, owner[0], owner[1], thread):
                raise ValueError
            artifacts = self._adapter().validate_json(json.dumps(envelope["artifacts"]))
            result = json.loads(row[2]) if row[2] is not None else None
            if envelope["node"] != row[3]:
                raise ValueError
            if result is None:
                if envelope["result"] is not None:
                    raise ValueError
            elif (
                result.get("artifact_digest") != row[0]
                or {k: v for k, v in result.items() if k != "artifact_digest"}
                != envelope["result"]
            ):
                raise ValueError
            return artifacts, result
        except Exception:
            raise ValueError("Private artifacts invalid.") from None

    def cached(self, state: "InvestigationState", node: str) -> bool:
        self.require_live(state)
        rows = self.read(
            "SELECT result FROM investigation_artifacts WHERE thread_id=? AND node=?",
            (state.thread_id, node),
        )
        row = rows[0] if rows else None
        if row is not None and row[0] is None:
            # Crash in an external call has no knowable result. Never rerun it.
            raise ValueError("Interrupted tool execution requires a new investigation.")
        return row is not None

    def begin_node(
        self,
        state: "InvestigationState",
        node: str,
        artifacts: "InvestigationArtifacts",
    ) -> None:
        with self.transaction() as db:
            self.require_live(state)
            self._write(db, state.thread_id, node, artifacts, None)

    def finish_node(
        self,
        state: "InvestigationState",
        node: str,
        artifacts: "InvestigationArtifacts",
        result: dict[str, object],
    ) -> None:
        with self.transaction() as db:
            self.require_live(state)
            db.execute(
                "DELETE FROM investigation_artifacts WHERE thread_id=? AND node=?",
                (state.thread_id, node),
            )
            self.crash(f"before_artifact_commit:{node}")
            self._write(db, state.thread_id, node, artifacts, result)
        self.crash("after_artifact_commit")
        self.crash(f"after_artifact_commit:{node}")

    def currency(self, state: "InvestigationState", *, accepted: bool) -> None:
        """Only called under BEGIN IMMEDIATE; no nested repository transactions."""
        if not self.repo._db.in_transaction:
            raise RuntimeError("Currency verification requires a transaction.")
        try:
            self._currency(state, accepted=accepted)
        except Exception:
            raise ValueError("Stale investigation inputs.") from None

    def _currency(self, state: "InvestigationState", *, accepted: bool) -> None:
        self.require_live(state)
        db = self.repo._db
        for source in state.sources:
            row = db.execute(
                "SELECT v.payload FROM investigation_active_sources a JOIN "
                "investigation_document_versions v "
                "USING(matter_id,document_id,version_id) "
                "JOIN matter_documents d ON d.id=a.document_id AND "
                "d.matter_id=a.matter_id "
                "WHERE a.matter_id=? AND a.document_id=? AND a.version_id=?",
                (state.matter_id, source.document_id, source.version_id),
            ).fetchone()
            if row is None:
                raise ValueError("Stale investigation inputs.")
            version = DocumentVersion.model_validate_json(row[0])
            if any(
                getattr(version, k) != getattr(source, k)
                for k in type(source).model_fields
            ):
                raise ValueError("Stale investigation inputs.")
            original, extracted = self.source_bytes(version)
            if (
                sha256(original).hexdigest() != source.original_content_hash
                or sha256(extracted).hexdigest() != source.extracted_text_hash
            ):
                raise ValueError("Stale investigation inputs.")
        if not state.dependencies:
            return
        dep = ProjectionDependency(
            matter_id=state.matter_id,
            projection_id=state.run_id,
            inputs=state.dependencies,
        )
        if not self.repo._dependency_current(dep, accepted=accepted):
            raise ValueError("Stale investigation inputs.")

    def source_bytes(self, version: DocumentVersion) -> tuple[bytes, bytes]:
        """Read bounded immutable files with pinned, no-follow ancestors."""
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        store = self.services.store
        root = os.open(store.private_root, flags)
        try:
            if not os.path.samestat(os.fstat(root), store._root_identity):
                raise ValueError("Private source root changed.")
            matter = os.open(version.matter_id, flags, dir_fd=root)
            try:
                document = os.open(version.document_id, flags, dir_fd=matter)
                try:
                    output = []
                    for name in (f"source.{version.document_type}", "extracted.txt"):
                        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=document)
                        with os.fdopen(fd, "rb") as source:
                            value = source.read(MAX_ARTIFACT_BYTES + 1)
                        if len(value) > MAX_ARTIFACT_BYTES:
                            raise ValueError("Private source budget exceeded.")
                        output.append(value)
                    return output[0], output[1]
                finally:
                    os.close(document)
            finally:
                os.close(matter)
        except (OSError, UnicodeError):
            raise ValueError("Trusted source bytes unavailable.") from None
        finally:
            os.close(root)

    @staticmethod
    def parse_approval(payload: str) -> "HumanApproval":
        from lextrace.investigations.workflow import HumanApproval

        try:
            return HumanApproval.model_validate_json(payload)
        except Exception:
            raise ValueError("Private approval intent invalid.") from None

    def intent(self, approval: "HumanApproval") -> tuple[str, str | None] | None:
        rows = self.read(
            "SELECT payload,status,outcome FROM investigation_intents "
            "WHERE thread_id=? AND gate=?",
            (approval.thread_id, approval.gate),
        )
        row = rows[0] if rows else None
        if row is None:
            return None
        if row[0] != approval.model_dump_json():
            raise ValueError("Conflicting or consumed human approval.")
        return row[1], row[2]

    def prepare_intent(
        self, state: "InvestigationState", approval: "HumanApproval"
    ) -> None:
        self.crash("before_intent")
        with self.transaction() as db:
            self.currency(state, accepted=approval.gate == "findings")
            db.execute(
                "INSERT INTO investigation_intents VALUES (?,?,?,?,?,?)",
                (
                    state.thread_id,
                    approval.gate,
                    approval.token,
                    approval.model_dump_json(),
                    "PREPARED",
                    None,
                ),
            )
        self.crash("after_intent")

    def apply_intent(
        self, state: "InvestigationState", approval: "HumanApproval"
    ) -> None:
        with self.transaction() as db:
            self.currency(state, accepted=approval.gate == "findings")
            self.crash("before_decisions")
            for decision in approval.evidence_decisions:
                self.repo.review_in_transaction(decision)
                self.crash("between_decisions")
            db.execute(
                "UPDATE investigation_intents SET status='APPLIED' WHERE "
                "thread_id=? AND gate=? AND status='PREPARED'",
                (state.thread_id, approval.gate),
            )
            self.crash("before_decision_commit")
        self.crash("after_decision_commit")

    def complete(self, approval: "HumanApproval", state: "InvestigationState") -> None:
        with self.transaction() as db:
            self.require_live(state)
            db.execute(
                "UPDATE investigation_intents SET status='DONE',outcome=? WHERE "
                "thread_id=? AND gate=?",
                (state.model_dump_json(), state.thread_id, approval.gate),
            )

    def cancel(self, state: "InvestigationState") -> None:
        with self.transaction() as db:
            self.require_live(state)
            db.execute(
                "UPDATE investigation_runs SET status='CANCELED' WHERE thread_id=?",
                (state.thread_id,),
            )
            db.execute(
                "DELETE FROM investigation_snapshots WHERE thread_id=?",
                (state.thread_id,),
            )

    def publish(
        self, state: "InvestigationState", artifacts: "InvestigationArtifacts"
    ) -> None:
        assert artifacts.snapshot is not None
        if state.approval_digest is None:
            raise ValueError("Human publication digest unavailable.")
        with self.transaction() as db:
            self.currency(state, accepted=True)
            rows = db.execute(
                "SELECT gate,payload,status FROM investigation_intents "
                "WHERE thread_id=?",
                (state.thread_id,),
            ).fetchall()
            intents = {
                gate: (self.parse_approval(payload), status)
                for gate, payload, status in rows
            }
            for gate, digest in (
                ("evidence", state.evidence_approval_digest),
                ("findings", state.approval_digest),
            ):
                if gate not in intents:
                    raise ValueError("Human publication intent unavailable.")
                intent, status = intents[gate]
                if (
                    status not in ("APPLIED", "DONE")
                    or intent.decision != "APPROVE"
                    or intent.digest != digest
                    or intent.gate != gate
                    or intent.matter_id != state.matter_id
                    or intent.run_id != state.run_id
                    or intent.thread_id != state.thread_id
                ):
                    raise ValueError("Invalid publication intent.")
            if artifacts.snapshot.reviewer_id != intents["findings"][0].reviewer_id:
                raise ValueError("Invalid publication signer.")
            # Recheck all original and retrieved account provenance, using trusted
            # bytes while source/review metadata writers are excluded by SQLite.
            references: list[EvidenceReference] = []
            for dependency in state.dependencies:
                revision = self.repo._latest(dependency.target)
                assert revision is not None
                original = revision.original
                references.extend(original.evidence_references)
                temporal = getattr(
                    original, "temporal", getattr(original, "stated_time", None)
                )
                if temporal is not None:
                    references.extend(temporal.evidence_references)
            if artifacts.result is not None:
                for finding in artifacts.result.findings:
                    references.extend(a.reference for a in finding.accounts)
            for reference in references:
                version = self.repo.active_document_version(
                    state.matter_id, reference.document_id
                )
                section = db.execute(
                    "SELECT payload FROM document_sections WHERE document_id=? AND "
                    "id=?",
                    (reference.document_id, reference.section_id),
                ).fetchone()
                if (
                    version is None
                    or section is None
                    or validate_evidence_reference(
                        reference,
                        version,
                        DocumentSection.model_validate_json(section[0]),
                        self.source_bytes(version)[1].decode("utf-8"),
                    )
                ):
                    raise ValueError("Invalid publication provenance.")
            payload = artifacts.snapshot.model_dump_json()
            existing = db.execute(
                "SELECT payload FROM investigation_snapshots WHERE thread_id=?",
                (state.thread_id,),
            ).fetchone()
            if existing is not None and existing[0] != payload:
                raise ValueError("Snapshot identity conflicts.")
            self.crash("before_publication_commit")
            db.execute(
                "INSERT OR IGNORE INTO investigation_snapshots VALUES (?,?,?)",
                (
                    state.thread_id,
                    sha256((state.run_id + state.approval_digest).encode()).hexdigest(),
                    payload,
                ),
            )
        self.crash("after_publication_commit")

    def cleanup(self) -> tuple[str, ...]:
        rows = self.read("SELECT thread_id FROM investigation_checkpoint_cleanup")
        for (thread,) in rows:
            self.saver.delete_thread(thread)
            with self.transaction() as db:
                db.execute(
                    "DELETE FROM investigation_checkpoint_cleanup WHERE thread_id=?",
                    (thread,),
                )
        return tuple(thread for (thread,) in rows)

"""M9 application boundary: current M7 publication + atomic SQLite human revisions."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from lextrace.deposition.contracts import (
    PlanBasis,
    PlanError,
    PlanRevision,
    PlanSummary,
    PlanView,
    QuestionDraft,
    ReviewPlan,
)
from lextrace.deposition.generation import digest, generate_basis
from lextrace.deposition.storage import PlanStorage, encode
from lextrace.investigations.contracts import (
    AttributedAssertion,
    EntityMention,
    EvidenceReference,
    InvestigationEvent,
)
from lextrace.investigations.durable import DurableRuntime
from lextrace.investigations.inconsistency_investigation import evidence_id
from lextrace.investigations.provenance import validate_evidence_reference
from lextrace.investigations.review import ReviewRevision, target_of
from lextrace.investigations.workflow import (
    ApprovedInvestigationSnapshot,
    InvestigationArtifacts,
    InvestigationState,
    InvestigationWorkflow,
)
from lextrace.matter.contracts import DocumentSection, MatterError


@dataclass
class PublishedContext:
    snapshot: ApprovedInvestigationSnapshot
    state: InvestigationState
    artifacts: InvestigationArtifacts
    revisions: tuple[ReviewRevision, ...]


class DepositionService:
    def __init__(self, workflow: InvestigationWorkflow) -> None:
        if workflow.runtime is None:
            raise PlanError("DEPOSITION_DURABILITY_REQUIRED", 503)
        self.workflow = workflow
        self.runtime: DurableRuntime = workflow.runtime

    @contextmanager
    def published(
        self, matter: str, thread: str, *, recover: bool = True
    ) -> Iterator[tuple[sqlite3.Connection, PublishedContext]]:
        try:
            self.workflow.services.store.require_investigation_matter(matter, thread)
        except MatterError:
            raise PlanError("DEPOSITION_NOT_FOUND", 404) from None
        try:
            if recover:
                self.workflow.reopen(thread)
            elif self.workflow.inspect(thread).stage != "APPROVED":
                raise PlanError("DEPOSITION_SOURCE_CONFLICT")
            snapshot = self.workflow.approved_snapshot(thread)
            state = self.workflow.inspect(thread)
            if snapshot.artifact_digest is None:
                raise PlanError("DEPOSITION_SNAPSHOT_CONFLICT")
            artifacts, _ = self.runtime.load(thread, digest=snapshot.artifact_digest)
            revisions = self.workflow.services.revisions(artifacts)
            # Current M4 reads own read transactions. Capture them first, then
            # recheck their exact signed dependencies under the writer fence.
            # SQLite fences source/review/deletion writers for the entire M9 operation.
            with self.runtime.transaction() as db:
                self.runtime.currency(state, accepted=True)
                row = db.execute(
                    "SELECT payload FROM investigation_snapshots WHERE "
                    "thread_id=? AND snapshot_id=?",
                    (thread, snapshot.snapshot_id),
                ).fetchone()
                if (
                    row is None
                    or row[0] != snapshot.model_dump_json()
                    or snapshot.matter_id != matter
                ):
                    raise PlanError("DEPOSITION_SNAPSHOT_CONFLICT")
                if snapshot.artifact_digest is None:
                    raise PlanError("DEPOSITION_SNAPSHOT_CONFLICT")
                if (
                    digest(
                        "".join(
                            link.model_dump_json() for link in artifacts.entity_links
                        )
                    )
                    != snapshot.entity_links_digest
                ):
                    raise PlanError("DEPOSITION_SNAPSHOT_CONFLICT")
                deps = {d.target: d.revision for d in snapshot.dependencies}
                if any(
                    r.review_state != "ACCEPTED"
                    or deps.get(target_of(r.original)) != r.revision
                    for r in revisions
                ):
                    raise PlanError("DEPOSITION_SOURCE_CONFLICT")
                if set(deps) != {target_of(r.original) for r in revisions}:
                    raise PlanError("DEPOSITION_SOURCE_CONFLICT")
                verified_originals = {
                    target_of(candidate): candidate
                    for result in artifacts.verified
                    for candidate in result.valid_candidates
                    if isinstance(
                        candidate,
                        AttributedAssertion | EntityMention | InvestigationEvent,
                    )
                }
                if any(
                    verified_originals.get(target_of(r.original)) != r.original
                    for r in revisions
                ):
                    raise PlanError("DEPOSITION_SOURCE_CONFLICT")
                for revision in revisions:
                    original = revision.original
                    for ref in original.evidence_references:
                        self.validate_reference(ref, snapshot)
                    temporal = getattr(
                        original, "stated_time", getattr(original, "temporal", None)
                    )
                    if temporal:
                        for ref in temporal.evidence_references:
                            self.validate_reference(ref, snapshot)
                if artifacts.result:
                    if (
                        tuple(f.finding_id for f in artifacts.result.findings)
                        != snapshot.finding_ids
                    ):
                        raise PlanError("DEPOSITION_SNAPSHOT_CONFLICT")
                    for finding in artifacts.result.findings:
                        for account in finding.accounts:
                            self.validate_reference(account.reference, snapshot)
                elif snapshot.finding_ids:
                    raise PlanError("DEPOSITION_SNAPSHOT_CONFLICT")
                yield db, PublishedContext(snapshot, state, artifacts, revisions)
        except ValueError:
            raise PlanError("DEPOSITION_SOURCE_CONFLICT") from None

    def validate_reference(
        self, ref: EvidenceReference, snapshot: ApprovedInvestigationSnapshot
    ) -> None:
        version = self.runtime.repo.active_document_version(
            snapshot.matter_id, ref.document_id
        )
        if version is None or not any(
            s.document_id == ref.document_id and s.version_id == ref.version_id
            for s in snapshot.sources
        ):
            raise PlanError("DEPOSITION_SOURCE_CONFLICT")
        rows = self.runtime.read(
            "SELECT payload FROM document_sections WHERE document_id=? AND id=?",
            (ref.document_id, ref.section_id),
        )
        if not rows or validate_evidence_reference(
            ref,
            version,
            DocumentSection.model_validate_json(rows[0][0]),
            self.runtime.source_bytes(version)[1].decode("utf-8"),
        ):
            raise PlanError("DEPOSITION_SOURCE_CONFLICT")

    @staticmethod
    def checked_basis(
        db: sqlite3.Connection, context: PublishedContext, plan_id: str
    ) -> PlanBasis:
        basis = PlanStorage.basis(db, plan_id, context.snapshot.thread_id)
        if basis != generate_basis(
            context.snapshot, context.artifacts, context.revisions, plan_id
        ):
            raise PlanError("DEPOSITION_SNAPSHOT_CONFLICT")
        return basis

    def create(
        self, matter: str, thread: str, snapshot_id: str, request_id: str, reviewer: str
    ) -> PlanView:
        with self.published(matter, thread) as (db, context):
            if context.snapshot.snapshot_id != snapshot_id:
                raise PlanError("DEPOSITION_SNAPSHOT_CONFLICT")
            prior = db.execute(
                "SELECT plan_id,snapshot_id FROM deposition_plans WHERE "
                "thread_id=? AND request_id=?",
                (thread, request_id),
            ).fetchone()
            if prior:
                if prior[1] != snapshot_id:
                    raise PlanError("DEPOSITION_RETRY_CONFLICT")
                return self._read(db, context, prior[0])
            if (
                db.execute(
                    "SELECT COUNT(*) FROM deposition_plans WHERE thread_id=?", (thread,)
                ).fetchone()[0]
                >= 20
            ):
                raise PlanError("DEPOSITION_PLAN_LIMIT")
            plan_id = digest("deposition-v1" + thread + snapshot_id + request_id)
            basis = generate_basis(
                context.snapshot, context.artifacts, context.revisions, plan_id
            )
            db.execute(
                "INSERT INTO deposition_plans VALUES (?,?,?,?,?,?)",
                (plan_id, thread, snapshot_id, request_id, *encode(basis)),
            )
            PlanStorage.append(
                db,
                PlanRevision(
                    plan_id=plan_id,
                    revision=1,
                    status="DRAFT",
                    questions=basis.questions,
                    reviewer_id=reviewer,
                    timestamp=datetime.now(UTC),
                    decision_id=request_id,
                    action="CREATE",
                ),
            )
            return self._read(db, context, plan_id)

    @staticmethod
    def validate_questions(
        basis: PlanBasis, questions: tuple[QuestionDraft, ...]
    ) -> None:
        originals = {q.question_id: q for q in basis.questions}
        if len(questions) != len(originals) or {
            q.question_id for q in questions
        } != set(originals):
            raise PlanError("DEPOSITION_INVALID_QUESTIONS", 422)
        if len({q.order for q in questions}) != len(questions):
            raise PlanError("DEPOSITION_INVALID_ORDER", 422)
        policies = {p.question_id: p for p in basis.policies}
        evidence = {e.evidence_id: e.reference for e in basis.evidence}
        for question in questions:
            original = originals[question.question_id]
            selected = set(question.evidence_ids)
            if (question.witness_id, question.category) != (
                original.witness_id,
                original.category,
            ) or (
                not selected.issubset(original.evidence_ids)
                or len(selected) != len(question.evidence_ids)
                or any(
                    not selected.intersection(g)
                    for g in policies[question.question_id].required_groups
                )
                or any(
                    k not in evidence or evidence_id(evidence[k]) != k for k in selected
                )
                or not question.wording.rstrip().endswith("?")
            ):
                raise PlanError("DEPOSITION_INVALID_EVIDENCE_SELECTION", 422)

    def _read(
        self, db: sqlite3.Connection, context: PublishedContext, plan_id: str
    ) -> PlanView:
        basis = self.checked_basis(db, context, plan_id)
        revisions = PlanStorage.revisions(db, plan_id)
        try:
            for revision in revisions:
                self.validate_questions(basis, revision.questions)
        except PlanError:
            raise PlanError("DEPOSITION_STORAGE_CONFLICT") from None
        return PlanView(basis=basis, current=revisions[-1])

    def read(self, matter: str, thread: str, plan_id: str) -> PlanView:
        with self.published(matter, thread) as (db, context):
            return self._read(db, context, plan_id)

    def list(self, matter: str, thread: str) -> tuple[PlanSummary, ...]:
        with self.published(matter, thread) as (db, context):
            rows = db.execute(
                "SELECT plan_id FROM deposition_plans WHERE thread_id=? "
                "AND snapshot_id=? ORDER BY rowid LIMIT 20",
                (thread, context.snapshot.snapshot_id),
            ).fetchall()
            result = []
            for row in rows:
                view = self._read(db, context, row[0])
                result.append(
                    PlanSummary(
                        plan_id=view.basis.plan_id,
                        snapshot_id=view.basis.snapshot_id,
                        revision=view.current.revision,
                        status=view.current.status,
                        outcome=view.basis.outcome,
                        witness_count=len(view.basis.witnesses),
                        question_count=len(view.current.questions),
                    )
                )
            return tuple(result)

    def history(
        self, matter: str, thread: str, plan_id: str
    ) -> tuple[PlanRevision, ...]:
        with self.published(matter, thread) as (db, context):
            self._read(db, context, plan_id)
            return PlanStorage.revisions(db, plan_id)

    def review(
        self, matter: str, thread: str, plan_id: str, request: ReviewPlan, reviewer: str
    ) -> PlanView:
        with self.published(matter, thread) as (db, context):
            view = self._read(db, context, plan_id)
            journal = request.model_dump_json() + "\n" + reviewer
            prior = db.execute(
                "SELECT payload,revision FROM deposition_decisions WHERE "
                "plan_id=? AND decision_id=?",
                (plan_id, request.decision_id),
            ).fetchone()
            if prior:
                if prior[0] != journal:
                    raise PlanError("DEPOSITION_RETRY_CONFLICT")
                revisions = PlanStorage.revisions(db, plan_id)
                return PlanView(basis=view.basis, current=revisions[prior[1] - 1])
            if view.current.revision != request.expected_revision:
                raise PlanError("DEPOSITION_REVISION_CONFLICT")
            if view.current.revision >= 200:
                raise PlanError("DEPOSITION_REVISION_LIMIT")
            self.validate_questions(view.basis, request.questions)
            if request.action == "APPROVE" and (
                not request.confirm
                or view.basis.outcome == "BLOCKED"
                or not any(q.disposition == "INCLUDE" for q in request.questions)
                or (
                    view.basis.outcome == "LIMITED"
                    and not request.acknowledge_limitations
                )
            ):
                raise PlanError("DEPOSITION_APPROVAL_REQUIRED", 422)
            status = {
                "APPROVE": "APPROVED",
                "REVISE": "DRAFT",
                "REJECT": "REJECTED",
                "DEFER": "DEFERRED",
            }[request.action]
            revision = PlanRevision.model_validate(
                dict(
                    plan_id=plan_id,
                    revision=view.current.revision + 1,
                    status=status,
                    questions=tuple(
                        sorted(
                            request.questions, key=lambda q: (q.order, q.question_id)
                        )
                    ),
                    reviewer_id=reviewer,
                    timestamp=datetime.now(UTC),
                    decision_id=request.decision_id,
                    action=request.action,
                    acknowledges_limitations=request.acknowledge_limitations,
                )
            )
            PlanStorage.append(db, revision)
            db.execute(
                "INSERT INTO deposition_decisions VALUES (?,?,?,?)",
                (plan_id, request.decision_id, journal, revision.revision),
            )
            return PlanView(basis=view.basis, current=revision)

    def approved_in_context(
        self,
        db: sqlite3.Connection,
        context: PublishedContext,
        plan_id: str,
        revision: int | None,
    ) -> PlanView:
        """Read an exact approved revision inside an existing publication fence.

        None requires the current revision to be approved; it never selects an
        older approval automatically. Explicit historical reads retain M9 semantics.
        """
        view = self._read(db, context, plan_id)
        history = PlanStorage.revisions(db, plan_id)
        if revision is None:
            if view.current.status != "APPROVED":
                raise PlanError("DEPOSITION_APPROVAL_REQUIRED")
            revision = view.current.revision
        if (
            revision < 1
            or revision > len(history)
            or history[revision - 1].status != "APPROVED"
        ):
            raise PlanError("DEPOSITION_APPROVAL_NOT_FOUND", 404)
        approved = history[revision - 1]
        return PlanView(
            basis=view.basis,
            current=approved.model_copy(
                update={
                    "questions": tuple(
                        q for q in approved.questions if q.disposition == "INCLUDE"
                    )
                }
            ),
        )

    def approved(
        self, matter: str, thread: str, plan_id: str, revision: int
    ) -> PlanView:
        with self.published(matter, thread) as (db, context):
            return self.approved_in_context(db, context, plan_id, revision)

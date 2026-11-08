"""Local LangGraph orchestration with optional private SQLite recovery.

Use open() for durable execution; injected MemorySaver remains session-only.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from hashlib import sha256
from threading import RLock
from typing import Annotated, Any, Literal, cast
from uuid import uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import Field, field_validator

from lextrace.investigations.chronology import Chronology, build_chronology
from lextrace.investigations.contracts import (
    SHA256,
    AttributedAssertion,
    EntityMention,
    InvestigationEvent,
    SourceIdentity,
)
from lextrace.investigations.durable import DurableRuntime
from lextrace.investigations.evidence_search import ApprovedVersion
from lextrace.investigations.extraction_verification import (
    ExtractionVerificationResult,
    verify_extraction,
)
from lextrace.investigations.factual_extraction import extract_factual_candidates
from lextrace.investigations.findings import (
    InvestigationBudgets,
    InvestigationDiagnostic,
    InvestigationResult,
)
from lextrace.investigations.inconsistency_candidates import (
    CandidateBudgets,
    CandidateInput,
    CandidatePair,
    select_inconsistency_candidates,
)
from lextrace.investigations.inconsistency_investigation import (
    InconsistencyInvestigationService,
)
from lextrace.investigations.repository import InvestigationRepository
from lextrace.investigations.review import (
    ProjectionDependency,
    RecordDependency,
    ReviewContract,
    ReviewDecision,
    ReviewRevision,
    ReviewTarget,
    apply_review,
    target_of,
)
from lextrace.investigations.reviewed_chronology import (
    ReviewedChronology,
    build_reviewed_chronology,
)
from lextrace.matter.store import MatterStore
from lextrace.research.llm import StructuredLLM

Count = Annotated[int, Field(ge=0, le=10000)]
Identity = Annotated[str, Field(min_length=1, max_length=256)]
Gate = Literal["evidence", "findings"]


class WorkflowBudgets(ReviewContract):
    max_documents: Annotated[int, Field(ge=1, le=100)] = 20
    extraction_windows: Annotated[int, Field(ge=0, le=100)] = 100
    extraction_records: Annotated[int, Field(ge=0, le=1000)] = 1000
    model_calls: Count = 140
    searches: Count = 20
    transitions: Count = 16
    candidates: CandidateBudgets = CandidateBudgets()
    investigation: InvestigationBudgets = InvestigationBudgets()


class InvestigationState(ReviewContract):
    matter_id: Identity
    run_id: Identity
    thread_id: Identity
    sources: Annotated[tuple[SourceIdentity, ...], Field(max_length=100)]
    budgets: WorkflowBudgets
    artifact_digest: SHA256 | None = None
    extraction_ids: tuple[Identity, ...] = ()
    extraction_revisions: tuple[Count, ...] = ()
    evidence_approval_digest: SHA256 | None = None
    coverage: tuple[Literal["COMPLETE", "PARTIAL", "OCR_REQUIRED"], ...] = ()
    dependencies: Annotated[tuple[RecordDependency, ...], Field(max_length=1000)] = ()
    entity_links_digest: SHA256 | None = None
    chronology_id: Identity | None = None
    candidate_ids: tuple[SHA256, ...] = ()
    finding_ids: tuple[SHA256, ...] = ()
    verification: tuple[
        Literal["INDEPENDENT_STRUCTURED_SUPPORT", "INSUFFICIENT"], ...
    ] = ()
    investigation_diagnostics: tuple[InvestigationDiagnostic, ...] = ()
    model_calls_reserved: Count = 0
    searches_used: Count = 0
    transitions_used: Count = 0
    errors: tuple[
        Literal[
            "SERVICE_FAILED",
            "STALE_INPUT",
            "BUDGET_EXHAUSTED",
            "UNACCEPTED_INPUT",
            "NO_EVIDENCE",
            "REJECTED",
        ],
        ...,
    ] = ()
    cancelled: bool = False
    review_gate: Gate | None = None
    pending_review_token: Identity | None = None
    approval_digest: SHA256 | None = None
    stage: Literal["RUNNING", "WAITING_REVIEW", "BLOCKED", "CANCELED", "APPROVED"] = (
        "RUNNING"
    )

    @field_validator(
        "sources",
        "extraction_ids",
        "extraction_revisions",
        "coverage",
        "dependencies",
        "candidate_ids",
        "finding_ids",
        "verification",
        "errors",
        "investigation_diagnostics",
        mode="before",
    )
    @classmethod
    def checkpoint_arrays(cls, value: object) -> object:
        """LangGraph msgpack restores tuple channels as arrays."""
        return tuple(value) if isinstance(value, list) else value


class HumanEntityLink(ReviewContract):
    account: ReviewTarget
    mentions: Annotated[tuple[ReviewTarget, ...], Field(min_length=1, max_length=20)]


class HumanApproval(ReviewContract):
    matter_id: Identity
    run_id: Identity
    thread_id: Identity
    gate: Gate
    token: Identity
    digest: SHA256
    reviewer_id: Identity
    entity_links: Annotated[tuple[HumanEntityLink, ...], Field(max_length=100)] = ()
    decision: Literal["APPROVE", "REJECT"]
    acknowledge_incomplete_coverage: bool = False
    # These are durable M4 commands, never generated or inferred by the graph.
    evidence_decisions: Annotated[
        tuple[ReviewDecision, ...], Field(max_length=1000)
    ] = ()


class ApprovedInvestigationSnapshot(ReviewContract):
    snapshot_id: SHA256
    source_dependency_digest: SHA256
    review_dependency_digest: SHA256
    artifact_digest: SHA256 | None
    matter_id: Identity
    run_id: Identity
    thread_id: Identity
    sources: tuple[SourceIdentity, ...]
    dependencies: tuple[RecordDependency, ...]
    chronology_id: Identity
    finding_ids: tuple[SHA256, ...]
    approval_digest: SHA256
    evidence_approval_digest: SHA256
    entity_links_digest: SHA256
    reviewer_id: Identity
    meaning: Literal["HUMAN_SIGNED_INVESTIGATION_HYPOTHESES"] = (
        "HUMAN_SIGNED_INVESTIGATION_HYPOTHESES"
    )


@dataclass
class InvestigationArtifacts:
    """Private run-local artifacts; deliberately excluded from graph checkpoints."""

    verified: tuple[ExtractionVerificationResult, ...] = ()
    chronology: Chronology | None = None
    reviewed_chronology: ReviewedChronology | None = None
    pairs: tuple[CandidatePair, ...] = ()
    result: InvestigationResult | None = None
    snapshot: ApprovedInvestigationSnapshot | None = None
    reviewer_id: str | None = None
    entity_links: tuple[HumanEntityLink, ...] = ()


class WorkflowBudgetExhausted(Exception):
    """Safe signal from a bounded service adapter."""


@dataclass
class InvestigationServices:
    """Typed adapters over the existing M3–M6 services, with no new algorithms."""

    store: MatterStore
    repository: InvestigationRepository
    extractor: StructuredLLM
    investigator: InconsistencyInvestigationService
    stopped: Callable[[], bool] = field(default=lambda: False)

    def currency(self, state: InvestigationState, *, accepted: bool = False) -> bool:
        try:
            return self._currency(state, accepted=accepted)
        except Exception:
            return False

    def _currency(self, state: InvestigationState, *, accepted: bool) -> bool:
        for source in state.sources:
            version = self.repository.active_document_version(
                state.matter_id, source.document_id
            )
            if version is None or any(
                getattr(version, k) != getattr(source, k)
                for k in SourceIdentity.model_fields
            ):
                return False
            # Recheck private bytes too; a manifest alone cannot detect file damage.
            text = self.store.document_text(state.matter_id, source.document_id)
            if sha256(text.encode()).hexdigest() != source.extracted_text_hash:
                return False
        for dependency in state.dependencies:
            current = self.repository.current_review(dependency.target)
            if current is None or current.revision != dependency.revision:
                return False
            if accepted and current.review_state != "ACCEPTED":
                return False
        return True

    def extract(
        self, state: InvestigationState
    ) -> tuple[ExtractionVerificationResult, ...]:
        results = []
        records = 0
        remaining = min(state.budgets.extraction_windows, state.budgets.model_calls)
        for source in state.sources:
            if self.stopped():
                raise ValueError("CANCELED")
            version = self.repository.active_document_version(
                state.matter_id, source.document_id
            )
            assert version is not None
            text = self.store.document_text(state.matter_id, source.document_id)
            sections = tuple(self.store.sections(state.matter_id, source.document_id))
            # Reserve all possible calls before entering M3 (including failures).
            windows = sum((len(s.text) + 19999) // 20000 for s in sections)
            allowance = min(remaining, windows)
            remaining -= allowance
            extracted = extract_factual_candidates(
                version,
                text,
                sections,
                self.extractor,
                run_id=state.run_id,
                max_windows=allowance,
                verbatim_anchors=True,
            )
            result = verify_extraction(
                extracted, version, sections, text, run_id=state.run_id
            )
            records += len(result.valid_candidates)
            if records > state.budgets.extraction_records:
                raise WorkflowBudgetExhausted
            for candidate in result.valid_candidates:
                if isinstance(
                    candidate, AttributedAssertion | InvestigationEvent | EntityMention
                ):
                    self.repository.register_review_candidate(candidate)
            results.append(result)
        return tuple(results)

    def revisions(
        self, artifacts: InvestigationArtifacts
    ) -> tuple[ReviewRevision, ...]:
        revisions = []
        for result in artifacts.verified:
            for candidate in result.valid_candidates:
                if isinstance(
                    candidate, AttributedAssertion | InvestigationEvent | EntityMention
                ):
                    current = self.repository.current_review(target_of(candidate))
                    if current is None:
                        raise ValueError("STALE_INPUT")
                    revisions.append(current)
        return tuple(revisions)


def _dependencies(
    revisions: tuple[ReviewRevision, ...],
) -> tuple[RecordDependency, ...]:
    return tuple(
        sorted(
            (
                RecordDependency(target=target_of(r.original), revision=r.revision)
                for r in revisions
            ),
            key=lambda d: d.model_dump_json(),
        )
    )


def _digest(state: InvestigationState) -> str:
    return sha256(
        state.model_dump_json(
            exclude={
                "pending_review_token",
                "approval_digest",
                "transitions_used",
                "stage",
            }
        ).encode()
    ).hexdigest()


class InvestigationWorkflow:
    """Local human interface; open() enables durable recovery and coordination.

    No arbitrary graph state is accepted from callers. This trusted local pilot
    interface is not an authentication API.
    """

    def __init__(
        self, services: InvestigationServices, *, checkpointer: BaseCheckpointSaver[Any]
    ) -> None:
        self.services = services
        self.artifacts: dict[str, InvestigationArtifacts] = {}
        self.runtime: DurableRuntime | None = None
        self._lock: RLock | DurableRuntime = RLock()
        self._used: set[str] = set()
        builder = StateGraph(InvestigationState)
        nodes = [
            ("validate_scope", self._validate),
            ("extract_verify", self._extract),
            ("prepare_chronology", self._chronology),
            ("prepare_evidence_gate", self._prepare_evidence),
            ("review_evidence", self._gate),
            ("load_accepted_reviews", self._accepted),
            ("select_candidates", self._select),
            ("investigate_context", self._investigate),
            ("prepare_findings_gate", self._prepare_findings),
            ("review_findings", self._gate),
            ("finalize_snapshot", self._finalize),
        ]
        for name, function in nodes:
            builder.add_node(name, cast(Any, self._bounded(function, name)))
        builder.add_edge(START, nodes[0][0])
        for index, (name, _) in enumerate(nodes):
            next_node = nodes[index + 1][0] if index + 1 < len(nodes) else END
            builder.add_conditional_edges(
                name,
                lambda s: "stop"
                if s.stage in ("BLOCKED", "CANCELED", "APPROVED")
                else "next",
                {"stop": END, "next": next_node},
            )
        self._graph = builder.compile(checkpointer=checkpointer)

    @classmethod
    def open(cls, services: InvestigationServices) -> "InvestigationWorkflow":
        """Open supported SQLite checkpoints under MatterStore.private_root.

        Reuse the same matter database/private root after restart. The caller owns
        services and must close this workflow before closing those services.
        """
        runtime = DurableRuntime(services)
        try:
            workflow = cls(services, checkpointer=runtime.saver)
            workflow.runtime = runtime
            workflow._lock = runtime
            with runtime:
                runtime.cleanup()
            return workflow
        except BaseException:
            runtime.close()
            raise

    def close(self) -> None:
        if self.runtime is not None:
            self.runtime.close()

    def _reload(self, state: InvestigationState) -> InvestigationState:
        runtime = self.runtime
        assert runtime is not None
        runtime.require_live(state)
        if state.artifact_digest is not None:
            rows = runtime.read(
                "SELECT node FROM investigation_artifacts WHERE thread_id=? "
                "AND digest=?",
                (state.thread_id, state.artifact_digest),
            )
            row = rows[0] if rows else None
            if row is None:
                raise ValueError("Private artifact revision unavailable.")
            referenced, _ = runtime.load(state.thread_id, row[0])
            self._validate_private(state, referenced)
        artifacts, _ = runtime.load(state.thread_id)
        # The private intent is authoritative for commands committed before the
        # graph advanced. Do not put these commands into checkpoint channels.
        rows = runtime.read(
            "SELECT payload,status FROM investigation_intents WHERE thread_id=? "
            "ORDER BY rowid",
            (state.thread_id,),
        )
        dependencies = {d.target: d for d in state.dependencies}
        for payload, status in rows:
            intent = runtime.parse_approval(payload)
            if (
                status not in ("PREPARED", "APPLIED", "DONE")
                or intent.matter_id != state.matter_id
                or intent.run_id != state.run_id
                or intent.thread_id != state.thread_id
            ):
                raise ValueError("Private approval intent invalid.")
            if status in ("APPLIED", "DONE"):
                self._used.add(intent.token)
                artifacts.reviewer_id = intent.reviewer_id
                if intent.gate == "evidence":
                    artifacts.entity_links = intent.entity_links
                for decision in intent.evidence_decisions:
                    dependencies[decision.target] = RecordDependency(
                        target=decision.target,
                        revision=decision.expected_revision + 1,
                    )
        checked = state.model_copy(
            update={"dependencies": tuple(dependencies.values())}
        )
        with runtime.transaction():
            runtime.currency(
                checked,
                accepted=state.review_gate == "findings" or state.stage == "APPROVED",
            )
        self.artifacts[state.thread_id] = artifacts
        return checked

    @staticmethod
    def _validate_private(
        state: InvestigationState, artifacts: InvestigationArtifacts
    ) -> None:
        invalid = (
            state.extraction_ids
            != tuple(r.coverage.original.coverage_id for r in artifacts.verified)
            or state.extraction_revisions
            != tuple(r.coverage.original.revision for r in artifacts.verified)
            or state.coverage != tuple(r.coverage.status for r in artifacts.verified)
            or state.candidate_ids != tuple(p.pair_id for p in artifacts.pairs)
        )
        if artifacts.result is not None:
            invalid |= (
                state.finding_ids
                != tuple(f.finding_id for f in artifacts.result.findings)
                or state.verification
                != tuple(f.verification for f in artifacts.result.findings)
                or set(artifacts.result.sources) != set(state.sources)
                or not set(artifacts.result.dependencies).issubset(state.dependencies)
            )
        elif state.review_gate == "findings" or state.stage == "APPROVED":
            invalid = True
        if state.entity_links_digest is not None:
            invalid |= (
                state.entity_links_digest
                != sha256(
                    "".join(
                        link.model_dump_json() for link in artifacts.entity_links
                    ).encode()
                ).hexdigest()
            )
        if invalid:
            raise ValueError("Private artifact revision conflicts with checkpoint.")

    def recover_cleanup(self) -> int:
        """Drain idempotent checkpoint cleanup queued by source/matter deletion."""
        if self.runtime is None:
            return 0
        with self._lock:
            threads = self.runtime.cleanup()
            for thread in threads:
                self.artifacts.pop(thread, None)
            return len(threads)

    def reopen(self, thread_id: str) -> InvestigationState:
        """Validate private recovery state and continue already-authorized work.

        A tool call interrupted before its result was durably recorded fails
        closed; recovery never guesses whether an external call completed.
        """
        if self.runtime is None:
            return self.inspect(thread_id)
        with self._lock:
            return self._reopen_locked(thread_id)

    def _reopen_locked(self, thread_id: str) -> InvestigationState:
        assert self.runtime is not None
        state = self.inspect(thread_id)
        if state.cancelled:
            return state
        self._reload(state)
        rows = self.runtime.read(
            "SELECT payload FROM investigation_intents WHERE thread_id=? "
            "AND status!='DONE' ORDER BY rowid LIMIT 1",
            (thread_id,),
        )
        row = rows[0] if rows else None
        if row:
            return self._resume_durable(self.runtime.parse_approval(row[0]))
        if self._graph.get_state(
            self._config(thread_id)
        ).next and not self.pending_gate(thread_id):
            self._graph.invoke(None, self._config(thread_id))
        return self.inspect(thread_id)

    def _resume_durable(self, approval: HumanApproval) -> InvestigationState:
        runtime = self.runtime
        assert runtime is not None
        state = self.inspect(approval.thread_id)
        if state.cancelled:
            raise ValueError("Investigation canceled.")
        self._reload(state)
        prior = runtime.intent(approval)
        if prior and prior[0] == "DONE":
            assert prior[1] is not None
            return InvestigationState.model_validate_json(prior[1])
        if prior:
            if prior[0] == "PREPARED":
                runtime.apply_intent(state, approval)
            self._used.add(approval.token)
            self.artifacts[state.thread_id].reviewer_id = approval.reviewer_id
            if approval.gate == "evidence":
                self.artifacts[state.thread_id].entity_links = approval.entity_links
            # A committed graph may already be at gate 2 or END. Resume only
            # the original interrupt, otherwise continue the remaining nodes.
            if state.pending_review_token == approval.token:
                self._graph.invoke(
                    Command(resume=approval.decision), self._config(state.thread_id)
                )
            elif self._graph.get_state(
                self._config(state.thread_id)
            ).next and not self.pending_gate(state.thread_id):
                self._graph.invoke(None, self._config(state.thread_id))
            runtime.crash("after_graph_continuation")
            result = self.inspect(state.thread_id)
            runtime.complete(approval, result)
            return result
        # New intent: use the existing M7A validation path before any writes.
        return self._resume(approval)

    @staticmethod
    def _config(thread_id: str) -> RunnableConfig:
        return {"configurable": {"thread_id": thread_id}, "recursion_limit": 32}

    def inspect(self, thread_id: str) -> InvestigationState:
        try:
            values = self._graph.get_state(self._config(thread_id)).values
            if not values:
                raise ValueError
            state = InvestigationState.model_validate(values)
        except Exception:
            raise ValueError("Unknown or invalid investigation checkpoint.") from None
        if self.runtime is not None:
            rows = self.runtime.read(
                "SELECT status FROM investigation_runs WHERE thread_id=? AND "
                "matter_id=? AND run_id=?",
                (thread_id, state.matter_id, state.run_id),
            )
            row = rows[0] if rows else None
            if row is None:
                self.artifacts.pop(thread_id, None)
                raise ValueError("Investigation deleted or unavailable.")
            if row[0] == "CANCELED":
                return state.model_copy(
                    update={
                        "cancelled": True,
                        "stage": "CANCELED",
                        "pending_review_token": None,
                    }
                )
        return state

    def pending_gate(self, thread_id: str) -> InvestigationState | None:
        state = self.inspect(thread_id)
        return (
            state
            if state.stage == "WAITING_REVIEW"
            and self._graph.get_state(self._config(thread_id)).interrupts
            else None
        )

    def start(
        self,
        matter_id: str,
        approved_versions: tuple[ApprovedVersion, ...],
        *,
        budgets: WorkflowBudgets | None = None,
        request_id: str | None = None,
        operator_id: str | None = None,
    ) -> InvestigationState:
        with self._lock:
            b = WorkflowBudgets.model_validate(
                (budgets or WorkflowBudgets()).model_dump(), strict=True
            )
            if not approved_versions or len(approved_versions) > b.max_documents:
                raise ValueError("Invalid source scope.")
            if len({v.document_id for v in approved_versions}) != len(
                approved_versions
            ):
                raise ValueError("Duplicate source scope.")
            sources = []
            for approved in approved_versions:
                version = self.services.repository.active_document_version(
                    matter_id, approved.document_id
                )
                if version is None or version.version_id != approved.version_id:
                    raise ValueError("Invalid source scope.")
                sources.append(
                    SourceIdentity.model_validate(
                        version.model_dump(include=set(SourceIdentity.model_fields))
                    )
                )
            start_request = None
            if request_id is not None:
                if (
                    self.runtime is None
                    or not operator_id
                    or re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", request_id) is None
                ):
                    raise ValueError("Invalid durable start request.")
                digest = sha256(
                    json.dumps(
                        {
                            "matter_id": matter_id,
                            "approved_versions": [
                                v.model_dump(mode="json") for v in approved_versions
                            ],
                            "budgets": b.model_dump(mode="json"),
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
                initial = self.runtime.start_request(
                    matter_id, request_id, operator_id, digest
                )
                if initial is not None:
                    if initial.sources != tuple(sources) or initial.budgets != b:
                        raise ValueError("Start request input conflicts.")
                    config = self._config(initial.thread_id)
                    if self._graph.get_state(config).values:
                        return self._reopen_locked(initial.thread_id)
                    if self.runtime.read(
                        "SELECT 1 FROM investigation_artifacts WHERE thread_id=? "
                        "AND node!='initial' LIMIT 1",
                        (initial.thread_id,),
                    ):
                        raise ValueError("Executed run checkpoint is unavailable.")
                    # The request/run/initial artifact committed before any graph
                    # checkpoint or provider call. Resume that exact initial run.
                    with self.runtime.transaction():
                        self.runtime.currency(initial, accepted=False)
                    self.artifacts[initial.thread_id] = self.runtime.load(
                        initial.thread_id, "initial"
                    )[0]
                    self._graph.invoke(initial, config)
                    return self.inspect(initial.thread_id)
                start_request = (request_id, operator_id, digest)
            thread = uuid4().hex
            state = InvestigationState(
                matter_id=matter_id,
                run_id=uuid4().hex,
                thread_id=thread,
                sources=tuple(sources),
                budgets=b,
            )
            self.artifacts[thread] = InvestigationArtifacts()
            if self.runtime is not None:
                self.runtime.create(
                    state, self.artifacts[thread], start_request=start_request
                )
            self._graph.invoke(state, self._config(thread))
            return self.inspect(thread)

    def resume(self, approval: HumanApproval) -> InvestigationState:
        try:
            approval = HumanApproval.model_validate(approval.model_dump(), strict=True)
        except Exception:
            raise ValueError("Invalid human approval.") from None
        if self.runtime is not None:
            with self._lock:
                return self._resume_durable(approval)
        return self._resume(approval)

    def _resume(self, approval: HumanApproval) -> InvestigationState:
        try:
            approval = HumanApproval.model_validate(approval.model_dump(), strict=True)
        except Exception:
            raise ValueError("Invalid human approval.") from None
        from contextlib import nullcontext

        with self._lock if self.runtime is None else nullcontext():
            state = self.pending_gate(approval.thread_id)
            if state is None or any(
                (
                    approval.matter_id != state.matter_id,
                    approval.run_id != state.run_id,
                    approval.gate != state.review_gate,
                    approval.token != state.pending_review_token,
                    approval.digest != state.approval_digest,
                    approval.token in self._used,
                )
            ):
                raise ValueError("Invalid or consumed human approval.")
            if not self.services.currency(state, accepted=approval.gate == "findings"):
                raise ValueError("Stale investigation inputs.")
            if approval.gate == "findings" and (
                approval.evidence_decisions or approval.entity_links
            ):
                raise ValueError("Evidence edits require a new investigation.")
            if (
                approval.decision == "APPROVE"
                and any(c != "COMPLETE" for c in state.coverage)
                and not approval.acknowledge_incomplete_coverage
            ):
                raise ValueError("Explicit coverage acknowledgement required.")
            allowed = {d.target for d in state.dependencies}
            for link in approval.entity_links:
                if (
                    link.account not in allowed
                    or link.account.kind == "mention"
                    or any(
                        m not in allowed or m.kind != "mention" for m in link.mentions
                    )
                ):
                    raise ValueError("Out of scope entity link.")
            if len({link.account for link in approval.entity_links}) != len(
                approval.entity_links
            ):
                raise ValueError("Duplicate entity link.")
            for decision in approval.evidence_decisions:
                if (
                    decision.target not in allowed
                    or decision.reviewer_id != approval.reviewer_id
                ):
                    raise ValueError("Out of scope review decision.")
            # Validate the complete ordered command batch without writes first.
            simulated = {
                target_of(r.original): r
                for r in self.services.revisions(self.artifacts[state.thread_id])
            }
            for decision in approval.evidence_decisions:
                simulated[decision.target] = apply_review(
                    simulated[decision.target], decision
                )
            # M4 handles optimistic revisions and idempotent decision IDs. Keep the
            # private commands outside checkpoint resume values (explanations may
            # contain private text). Crash reconciliation is deliberately M7B.
            if self.runtime is not None:
                self.runtime.prepare_intent(state, approval)
                self.runtime.apply_intent(state, approval)
            else:
                for decision in approval.evidence_decisions:
                    self.services.repository.review(decision)
            if approval.gate == "evidence":
                self.artifacts[state.thread_id].entity_links = approval.entity_links
            self._used.add(approval.token)
            self.artifacts[state.thread_id].reviewer_id = approval.reviewer_id
            self._graph.invoke(
                Command(resume=approval.decision), self._config(state.thread_id)
            )
            result = self.inspect(state.thread_id)
            if self.runtime is not None:
                self.runtime.crash("after_graph_continuation")
                self.runtime.complete(approval, result)
            return result

    def approved_snapshot(self, thread_id: str) -> ApprovedInvestigationSnapshot:
        """Read a current signed hypothesis snapshot; stale history is not current."""
        with self._lock:
            state = self.inspect(thread_id)
            if state.stage != "APPROVED":
                raise ValueError("Investigation is not approved.")
            if self.runtime is None:
                if not self.services.currency(state, accepted=True):
                    raise ValueError("Stale investigation inputs.")
            else:
                self._reload(state)
                with self.runtime.transaction() as db:
                    self.runtime.currency(state, accepted=True)
                    row = db.execute(
                        "SELECT payload FROM investigation_snapshots WHERE thread_id=?",
                        (thread_id,),
                    ).fetchone()
                    if row is None:
                        raise ValueError("Approved snapshot unavailable.")
                    return ApprovedInvestigationSnapshot.model_validate_json(row[0])
            snapshot = self.artifacts[thread_id].snapshot
            if snapshot is None:
                raise ValueError("Approved snapshot unavailable.")
            return snapshot

    def cancel(self, thread_id: str) -> InvestigationState:
        with self._lock:
            state = self.inspect(thread_id)
            if state.stage == "APPROVED":
                raise ValueError("Investigation already approved.")
            if state.cancelled:
                return state
            if self.runtime is not None:
                self.runtime.cancel(state)
            self._graph.update_state(
                self._config(thread_id),
                {"cancelled": True, "stage": "CANCELED", "pending_review_token": None},
            )
            return self.inspect(thread_id)

    def _bounded(
        self, function: Callable[[InvestigationState], dict[str, object]], name: str
    ) -> Callable[[InvestigationState], dict[str, object]]:
        def run(state: InvestigationState) -> dict[str, object]:
            if self.runtime is not None:
                self.runtime.require_live(state)
                if self.runtime.cached(state, name):
                    artifacts, cached = self.runtime.load(state.thread_id, name)
                    self.artifacts[state.thread_id] = artifacts
                    assert cached is not None
                    return cached
                if name in ("extract_verify", "investigate_context"):
                    self.runtime.begin_node(
                        state, name, self.artifacts[state.thread_id]
                    )
            if state.cancelled or self.services.stopped():
                return {"stage": "CANCELED", "cancelled": True}
            if state.transitions_used >= state.budgets.transitions:
                return {"stage": "BLOCKED", "errors": ("BUDGET_EXHAUSTED",)}
            try:
                result = function(state)
            except GraphInterrupt:
                raise
            except WorkflowBudgetExhausted:
                result = {"stage": "BLOCKED", "errors": ("BUDGET_EXHAUSTED",)}
            except Exception:
                # Exception payloads can contain private text; expose safe codes.
                result = {"stage": "BLOCKED", "errors": ("SERVICE_FAILED",)}
            if self.services.stopped():
                result = {"stage": "CANCELED", "cancelled": True}
                self.artifacts[state.thread_id].snapshot = None
            result["transitions_used"] = state.transitions_used + 1
            if self.runtime is not None:
                self.runtime.finish_node(
                    state, name, self.artifacts[state.thread_id], result
                )
            return result

        return run

    def _validate(self, state: InvestigationState) -> dict[str, object]:
        if not self.services.currency(state):
            return {"stage": "BLOCKED", "errors": ("STALE_INPUT",)}
        return {}

    def _extract(self, state: InvestigationState) -> dict[str, object]:
        artifacts = self.artifacts[state.thread_id]
        artifacts.verified = self.services.extract(state)
        # Reserve the total extraction allowance; unused reservations deliberately
        # do not expand subsequent tool budgets.
        return {
            "extraction_ids": tuple(
                r.coverage.original.coverage_id for r in artifacts.verified
            ),
            "extraction_revisions": tuple(
                r.coverage.original.revision for r in artifacts.verified
            ),
            "coverage": tuple(r.coverage.status for r in artifacts.verified),
            "dependencies": _dependencies(self.services.revisions(artifacts)),
            "model_calls_reserved": min(
                state.budgets.extraction_windows, state.budgets.model_calls
            ),
        }

    def _chronology(self, state: InvestigationState) -> dict[str, object]:
        self.artifacts[state.thread_id].chronology = build_chronology(
            self.artifacts[state.thread_id].verified, matter_id=state.matter_id
        )
        return {
            "chronology_id": sha256(
                (state.run_id + "chronology-v1").encode()
            ).hexdigest()
        }

    def _prepare(self, state: InvestigationState, gate: Gate) -> dict[str, object]:
        if not self.services.currency(state, accepted=gate == "findings"):
            return {"stage": "BLOCKED", "errors": ("STALE_INPUT",)}
        prepared = state.model_copy(update={"review_gate": gate})
        return {
            "review_gate": gate,
            "pending_review_token": uuid4().hex,
            "approval_digest": _digest(prepared),
            "stage": "WAITING_REVIEW",
        }

    def _prepare_evidence(self, state: InvestigationState) -> dict[str, object]:
        return self._prepare(state, "evidence")

    def _prepare_findings(self, state: InvestigationState) -> dict[str, object]:
        return self._prepare(state, "findings")

    def _gate(self, state: InvestigationState) -> dict[str, object]:
        # No writes before interrupt; LangGraph re-enters only this cheap node.
        checked = (
            state
            if state.review_gate == "findings"
            else state.model_copy(update={"dependencies": ()})
        )
        if not self.services.currency(
            checked, accepted=state.review_gate == "findings"
        ):
            return {"stage": "BLOCKED", "errors": ("STALE_INPUT",)}
        answer = interrupt(
            {
                "gate": state.review_gate,
                "digest": state.approval_digest,
                "token": state.pending_review_token,
            }
        )
        if state.pending_review_token not in self._used:
            return {"stage": "BLOCKED", "errors": ("REJECTED",)}
        if answer != "APPROVE":
            return {
                "stage": "BLOCKED",
                "errors": ("REJECTED",),
                "pending_review_token": None,
            }
        return {
            "stage": "RUNNING",
            "pending_review_token": None,
            "evidence_approval_digest": state.approval_digest
            if state.review_gate == "evidence"
            else state.evidence_approval_digest,
        }

    def _accepted(self, state: InvestigationState) -> dict[str, object]:
        artifacts = self.artifacts[state.thread_id]
        revisions = self.services.revisions(artifacts)
        if not revisions:
            return {"stage": "BLOCKED", "errors": ("NO_EVIDENCE",)}
        if any(r.review_state != "ACCEPTED" for r in revisions):
            return {"stage": "BLOCKED", "errors": ("UNACCEPTED_INPUT",)}
        dependencies = _dependencies(revisions)
        updated = state.model_copy(update={"dependencies": dependencies})
        if not self.services.currency(updated, accepted=True):
            return {"stage": "BLOCKED", "errors": ("STALE_INPUT",)}
        artifacts.reviewed_chronology = build_reviewed_chronology(
            tuple(r for r in revisions if isinstance(r.original, InvestigationEvent)),
            matter_id=state.matter_id,
            require_accepted=True,
        )
        identity = sha256(
            (state.run_id + "".join(d.model_dump_json() for d in dependencies)).encode()
        ).hexdigest()
        self.services.repository.save_projection_dependency(
            ProjectionDependency(
                matter_id=state.matter_id, projection_id=identity, inputs=dependencies
            )
        )
        return {
            "dependencies": dependencies,
            "chronology_id": identity,
            "entity_links_digest": sha256(
                "".join(
                    link.model_dump_json() for link in artifacts.entity_links
                ).encode()
            ).hexdigest(),
        }

    def _select(self, state: InvestigationState) -> dict[str, object]:
        if not self.services.currency(state, accepted=True):
            return {"stage": "BLOCKED", "errors": ("STALE_INPUT",)}
        artifacts = self.artifacts[state.thread_id]
        revisions = self.services.revisions(artifacts)
        by_target = {target_of(r.original): r for r in revisions}
        links = {link.account: link.mentions for link in artifacts.entity_links}
        inputs = tuple(
            CandidateInput(
                revision=r,
                entity_mentions=tuple(
                    by_target[t] for t in links.get(target_of(r.original), ())
                ),
            )
            for r in revisions
            if isinstance(r.original, AttributedAssertion | InvestigationEvent)
        )
        selection = select_inconsistency_candidates(
            inputs,
            matter_id=state.matter_id,
            sources=state.sources,
            verified=artifacts.verified,
            current=state.dependencies,
            budgets=state.budgets.candidates,
        )
        if selection.partial:
            return {"stage": "BLOCKED", "errors": ("BUDGET_EXHAUSTED",)}
        artifacts.pairs = selection.pairs
        return {"candidate_ids": tuple(p.pair_id for p in selection.pairs)}

    def _investigate(self, state: InvestigationState) -> dict[str, object]:
        if not self.services.currency(state, accepted=True):
            return {"stage": "BLOCKED", "errors": ("STALE_INPUT",)}
        artifacts = self.artifacts[state.thread_id]
        budgets = state.budgets.investigation.model_copy(
            update={
                "max_model_calls": min(
                    state.budgets.investigation.max_model_calls,
                    state.budgets.model_calls - state.model_calls_reserved,
                ),
                "max_searches": min(
                    state.budgets.searches, state.budgets.investigation.max_searches
                ),
            }
        )
        result = self.services.investigator.investigate(
            artifacts.pairs,
            matter_id=state.matter_id,
            approved_versions=tuple(
                ApprovedVersion(document_id=s.document_id, version_id=s.version_id)
                for s in state.sources
            ),
            budgets=budgets,
        )
        artifacts.result = result
        if result.partial and any(
            d.code != "INSUFFICIENT_SUPPORT" for d in result.diagnostics
        ):
            return {
                "stage": "BLOCKED",
                "investigation_diagnostics": result.diagnostics,
                "searches_used": result.searches,
                "model_calls_reserved": state.model_calls_reserved + result.model_calls,
                "errors": (
                    "BUDGET_EXHAUSTED"
                    if any(d.code == "BUDGET_EXHAUSTED" for d in result.diagnostics)
                    else "SERVICE_FAILED",
                ),
            }
        return {
            "investigation_diagnostics": result.diagnostics,
            "finding_ids": tuple(f.finding_id for f in result.findings),
            "verification": tuple(f.verification for f in result.findings),
            "searches_used": result.searches,
            "model_calls_reserved": state.model_calls_reserved + result.model_calls,
        }

    def _finalize(self, state: InvestigationState) -> dict[str, object]:
        if not self.services.currency(state, accepted=True):
            return {"stage": "BLOCKED", "errors": ("STALE_INPUT",)}
        artifacts = self.artifacts[state.thread_id]
        assert state.chronology_id and state.approval_digest and artifacts.reviewer_id
        assert state.evidence_approval_digest and state.entity_links_digest
        artifacts.snapshot = ApprovedInvestigationSnapshot(
            snapshot_id=sha256(
                (state.run_id + state.approval_digest).encode()
            ).hexdigest(),
            source_dependency_digest=sha256(
                "".join(s.model_dump_json() for s in state.sources).encode()
            ).hexdigest(),
            review_dependency_digest=sha256(
                "".join(d.model_dump_json() for d in state.dependencies).encode()
            ).hexdigest(),
            artifact_digest=state.artifact_digest,
            matter_id=state.matter_id,
            run_id=state.run_id,
            thread_id=state.thread_id,
            sources=state.sources,
            dependencies=state.dependencies,
            chronology_id=state.chronology_id,
            finding_ids=state.finding_ids,
            approval_digest=state.approval_digest,
            evidence_approval_digest=state.evidence_approval_digest,
            entity_links_digest=state.entity_links_digest,
            reviewer_id=artifacts.reviewer_id,
        )
        if self.runtime is not None:
            self.runtime.publish(state, artifacts)
        return {"stage": "APPROVED", "review_gate": None}

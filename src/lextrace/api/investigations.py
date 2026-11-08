"""Authenticated adapters for the existing durable investigation workflow."""

import json
from collections.abc import Callable, Coroutine
from typing import Annotated, Any, Literal, TypeVar, cast

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
)

from lextrace.api.auth import OperatorPrincipal, require_operator
from lextrace.investigations.chronology import Chronology
from lextrace.investigations.evidence_search import ApprovedVersion
from lextrace.investigations.repository import InvestigationRepository
from lextrace.investigations.review import DateCorrection, ReviewDecision, ReviewTarget
from lextrace.investigations.reviewed_chronology import ReviewedChronology
from lextrace.investigations.workflow import (
    ApprovedInvestigationSnapshot,
    HumanApproval,
    HumanEntityLink,
    InvestigationState,
    InvestigationWorkflow,
    WorkflowBudgets,
)
from lextrace.matter.contracts import MatterError
from lextrace.matter.store import MatterStore

Contract = TypeVar("Contract", bound=BaseModel)


def wire_contract(value: object, model: type[Contract]) -> Contract:
    """Validate JSON arrays/dates in strict domain contracts as JSON, not Python."""
    return (
        value
        if isinstance(value, model)
        else model.model_validate_json(json.dumps(value))
    )


class InvestigationRoute(APIRoute):
    """Stable error envelopes also cover dependency and request validation errors."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def guarded(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError:
                raise HTTPException(422, "INVESTIGATION_INVALID_REQUEST") from None
            except HTTPException:
                raise
            except ValueError:
                raise HTTPException(409, "INVESTIGATION_STATE_CONFLICT") from None
            except Exception:
                raise HTTPException(503, "INVESTIGATION_UNAVAILABLE") from None

        return guarded


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StartInvestigation(RequestModel):
    request_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:-]{1,256}$")] | None = None
    approved_versions: Annotated[
        list[ApprovedVersion], Field(min_length=1, max_length=100)
    ]
    budgets: Annotated[
        WorkflowBudgets, BeforeValidator(lambda v: wire_contract(v, WorkflowBudgets))
    ] = WorkflowBudgets()


class EvidenceDecision(RequestModel):
    decision_id: str
    timestamp: AwareDatetime
    target: ReviewTarget
    expected_revision: int
    decision: Literal["ACCEPT", "REJECT", "DEFER", "EDIT"]
    explanation: str | None = None
    date_correction: (
        Annotated[
            DateCorrection, BeforeValidator(lambda v: wire_contract(v, DateCorrection))
        ]
        | None
    ) = None
    resolved_entity_id: str | None = None


class ReviewInvestigation(RequestModel):
    run_id: str
    gate: Literal["evidence", "findings"]
    token: str
    digest: str
    decision: Literal["APPROVE", "REJECT"]
    acknowledge_incomplete_coverage: bool = False
    entity_links: Annotated[
        list[
            Annotated[
                HumanEntityLink,
                BeforeValidator(lambda v: wire_contract(v, HumanEntityLink)),
            ]
        ],
        Field(max_length=100),
    ] = []
    evidence_decisions: Annotated[list[EvidenceDecision], Field(max_length=1000)] = []


class InvestigationView(RequestModel):
    matter_id: str
    thread_id: str
    run_id: str
    stage: Literal["RUNNING", "WAITING_REVIEW", "BLOCKED", "CANCELED", "APPROVED"]
    errors: tuple[str, ...]
    coverage: tuple[str, ...]
    checkpoint: Literal["evidence", "findings"] | None
    token: str | None
    digest: str | None
    review: dict[str, JsonValue] | None
    snapshot_id: str | None


def investigation_router(
    store: Callable[[], MatterStore],
    workflow: Callable[[], InvestigationWorkflow],
    repository: Callable[[], InvestigationRepository],
) -> APIRouter:
    router = APIRouter(
        prefix="/matters/{matter_id}/investigations", route_class=InvestigationRoute
    )

    def matter(matter_id: str) -> None:
        try:
            found = store().get_matter(matter_id)
        except MatterError:
            found = None
        if found is None:
            raise HTTPException(404, "INVESTIGATION_NOT_FOUND")

    def owned(matter_id: str, thread_id: str) -> None:
        try:
            store().require_investigation_matter(matter_id, thread_id)
        except MatterError:
            raise HTTPException(404, "INVESTIGATION_NOT_FOUND") from None

    def view(w: InvestigationWorkflow, state: InvestigationState) -> InvestigationView:
        pending = w.pending_gate(state.thread_id)
        review: dict[str, JsonValue] | None = None
        if pending:
            artifacts = w.artifacts[state.thread_id]
            review = {
                "records": [
                    r.model_dump(mode="json") for r in w.services.revisions(artifacts)
                ],
                "chronology": TypeAdapter(ReviewedChronology).dump_python(
                    artifacts.reviewed_chronology, mode="json"
                )
                if artifacts.reviewed_chronology
                else TypeAdapter(Chronology).dump_python(
                    artifacts.chronology, mode="json"
                )
                if artifacts.chronology
                else None,
                "findings": artifacts.result.model_dump(mode="json")
                if artifacts.result
                else None,
            }
        snapshot = (
            w.approved_snapshot(state.thread_id) if state.stage == "APPROVED" else None
        )
        return InvestigationView(
            matter_id=state.matter_id,
            thread_id=state.thread_id,
            run_id=state.run_id,
            stage=state.stage,
            errors=state.errors,
            coverage=state.coverage,
            checkpoint=pending.review_gate if pending else None,
            token=pending.pending_review_token if pending else None,
            digest=pending.approval_digest if pending else None,
            review=review,
            snapshot_id=snapshot.snapshot_id if snapshot else None,
        )

    def safe(action: Callable[[], InvestigationView]) -> InvestigationView:
        try:
            return action()
        except HTTPException:
            raise
        except ValueError:
            raise HTTPException(409, "INVESTIGATION_STATE_CONFLICT") from None
        except Exception:
            raise HTTPException(503, "INVESTIGATION_UNAVAILABLE") from None

    @router.post("", status_code=201)
    def start(
        matter_id: str,
        body: StartInvestigation,
        principal: Annotated[OperatorPrincipal, Depends(require_operator)],
    ) -> InvestigationView:
        matter(matter_id)

        def execute() -> InvestigationView:
            w = workflow()
            return view(
                w,
                w.start(
                    matter_id,
                    tuple(body.approved_versions),
                    budgets=body.budgets,
                    request_id=body.request_id,
                    operator_id=principal.operator_id,
                ),
            )

        return safe(execute)

    @router.get("")
    def listing(matter_id: str) -> list[InvestigationView]:
        matter(matter_id)
        w = workflow()
        return [
            read(matter_id, thread)
            for thread in w.services.repository.list_threads(matter_id)
        ]

    @router.get("/eligible-versions")
    def eligible_versions(matter_id: str) -> list[ApprovedVersion]:
        """Selectable active READY sources; not a persisted human approval state."""
        matter(matter_id)
        repo = repository()
        result = []
        for document in sorted(
            store().list_documents(matter_id), key=lambda item: item.document_id
        ):
            if document.ingestion_status != "READY":
                continue
            version = repo.active_document_version(matter_id, document.document_id)
            if version is not None:
                result.append(
                    ApprovedVersion(
                        document_id=document.document_id, version_id=version.version_id
                    )
                )
        return result

    @router.get("/{thread_id}")
    def read(matter_id: str, thread_id: str) -> InvestigationView:
        owned(matter_id, thread_id)

        def execute() -> InvestigationView:
            w = workflow()
            return view(w, w.reopen(thread_id))

        return safe(execute)

    @router.post("/{thread_id}/decisions")
    def decide(
        matter_id: str,
        thread_id: str,
        body: ReviewInvestigation,
        principal: Annotated[OperatorPrincipal, Depends(require_operator)],
    ) -> InvestigationView:
        owned(matter_id, thread_id)

        def execute() -> InvestigationView:
            w = workflow()
            try:
                approval = HumanApproval.model_validate(
                    {
                        **body.model_dump(
                            exclude={"evidence_decisions", "entity_links"}
                        ),
                        "matter_id": matter_id,
                        "thread_id": thread_id,
                        "reviewer_id": principal.operator_id,
                        "entity_links": tuple(body.entity_links),
                        "evidence_decisions": tuple(
                            ReviewDecision.model_validate(
                                {
                                    **d.model_dump(),
                                    "reviewer_id": principal.operator_id,
                                }
                            )
                            for d in body.evidence_decisions
                        ),
                    }
                )
            except ValidationError:
                raise HTTPException(422, "INVESTIGATION_INVALID_REQUEST") from None
            w.resume(approval)
            return view(w, w.reopen(thread_id))

        return safe(execute)

    @router.get("/{thread_id}/snapshots/{snapshot_id}")
    def snapshot(
        matter_id: str, thread_id: str, snapshot_id: str
    ) -> ApprovedInvestigationSnapshot:
        owned(matter_id, thread_id)
        try:
            w = workflow()
            w.reopen(thread_id)
            result = w.approved_snapshot(thread_id)
            if result.snapshot_id != snapshot_id:
                raise HTTPException(404, "INVESTIGATION_ARTIFACT_NOT_FOUND")
            return result
        except ValueError:
            raise HTTPException(409, "INVESTIGATION_STATE_CONFLICT") from None
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "INVESTIGATION_UNAVAILABLE") from None

    @router.get("/{thread_id}/snapshots/{snapshot_id}/artifacts/{artifact_id}")
    def artifact(
        matter_id: str, thread_id: str, snapshot_id: str, artifact_id: str
    ) -> dict[str, JsonValue]:
        signed = snapshot(matter_id, thread_id, snapshot_id)
        runtime = workflow().runtime
        if runtime is None or signed.artifact_digest is None:
            raise HTTPException(409, "INVESTIGATION_STATE_CONFLICT")
        # The snapshot signs this exact journal revision, not the mutable cache
        # or the latest (post-publication) journal entry.
        artifacts, _ = runtime.load(thread_id, digest=signed.artifact_digest)
        if artifact_id == signed.chronology_id and artifacts.reviewed_chronology:
            return cast(
                dict[str, JsonValue],
                TypeAdapter(ReviewedChronology).dump_python(
                    artifacts.reviewed_chronology, mode="json"
                ),
            )
        if artifact_id in signed.finding_ids and artifacts.result:
            for finding in artifacts.result.findings:
                if finding.finding_id == artifact_id:
                    return finding.model_dump(mode="json")
        raise HTTPException(404, "INVESTIGATION_ARTIFACT_NOT_FOUND")

    return router

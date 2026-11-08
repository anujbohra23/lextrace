"""Authenticated, matter/thread-scoped M9 adapters; identity never comes from JSON."""

from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from lextrace.api.auth import OperatorPrincipal, require_operator
from lextrace.api.investigations import wire_contract
from lextrace.deposition.contracts import (
    PlanError,
    PlanRevision,
    PlanSummary,
    PlanView,
    ReviewPlan,
    WitnessDossier,
)
from lextrace.deposition.service import DepositionService
from lextrace.investigations.workflow import InvestigationWorkflow
from lextrace.matter.contracts import MatterError
from lextrace.matter.store import MatterStore


class DepositionRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def guarded(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError:
                raise HTTPException(422, "DEPOSITION_INVALID_REQUEST") from None
            except PlanError as error:
                raise HTTPException(error.status, error.code) from None
            except HTTPException:
                raise
            except ValueError:
                raise HTTPException(409, "DEPOSITION_SOURCE_CONFLICT") from None
            except Exception:
                raise HTTPException(503, "DEPOSITION_UNAVAILABLE") from None

        return guarded


class CreatePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshot_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    request_id: Annotated[str, Field(min_length=1, max_length=256)]


class PlanReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review: Annotated[
        ReviewPlan, BeforeValidator(lambda value: wire_contract(value, ReviewPlan))
    ]


def deposition_router(
    store: Callable[[], MatterStore], workflow: Callable[[], InvestigationWorkflow]
) -> APIRouter:
    router = APIRouter(
        prefix="/matters/{matter_id}/investigations/{thread_id}/deposition-plans",
        route_class=DepositionRoute,
    )

    def service(matter_id: str, thread_id: str) -> DepositionService:
        try:
            store().require_investigation_matter(matter_id, thread_id)
        except MatterError:
            raise PlanError("DEPOSITION_NOT_FOUND", 404) from None
        return DepositionService(workflow())

    @router.post("", status_code=201)
    def create(
        matter_id: str,
        thread_id: str,
        body: CreatePlan,
        principal: Annotated[OperatorPrincipal, Depends(require_operator)],
    ) -> PlanView:
        return service(matter_id, thread_id).create(
            matter_id,
            thread_id,
            body.snapshot_id,
            body.request_id,
            principal.operator_id,
        )

    @router.get("")
    def listing(matter_id: str, thread_id: str) -> tuple[PlanSummary, ...]:
        return service(matter_id, thread_id).list(matter_id, thread_id)

    @router.get("/{plan_id}")
    def read(matter_id: str, thread_id: str, plan_id: str) -> PlanView:
        return service(matter_id, thread_id).read(matter_id, thread_id, plan_id)

    @router.get("/{plan_id}/witnesses")
    def witnesses(
        matter_id: str, thread_id: str, plan_id: str
    ) -> tuple[WitnessDossier, ...]:
        return read(matter_id, thread_id, plan_id).basis.witnesses

    @router.post("/{plan_id}/review")
    def review(
        matter_id: str,
        thread_id: str,
        plan_id: str,
        body: PlanReviewRequest,
        principal: Annotated[OperatorPrincipal, Depends(require_operator)],
    ) -> PlanView:
        # Input validation is confined to the wire model; storage failures never
        # masquerade as client validation errors.
        return service(matter_id, thread_id).review(
            matter_id, thread_id, plan_id, body.review, principal.operator_id
        )

    @router.get("/{plan_id}/history")
    def history(
        matter_id: str, thread_id: str, plan_id: str
    ) -> tuple[PlanRevision, ...]:
        return service(matter_id, thread_id).history(matter_id, thread_id, plan_id)

    @router.get("/{plan_id}/approved/{revision}")
    def approved(
        matter_id: str, thread_id: str, plan_id: str, revision: int
    ) -> PlanView:
        return service(matter_id, thread_id).approved(
            matter_id, thread_id, plan_id, revision
        )

    return router

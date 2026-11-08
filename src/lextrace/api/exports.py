"""Authenticated in-memory DOCX attachment routes. No navigation bearer bypass."""

from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

from lextrace.api.auth import OperatorPrincipal, require_operator
from lextrace.deposition.contracts import PlanError
from lextrace.deposition.service import DepositionService
from lextrace.exports.document import DOCX_TYPE, Download, ExportError
from lextrace.exports.service import ExportService
from lextrace.investigations.workflow import InvestigationWorkflow
from lextrace.matter.contracts import Matter, MatterError
from lextrace.matter.store import MatterStore


class ExportRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def guarded(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError:
                raise HTTPException(422, "EXPORT_INVALID_REQUEST") from None
            except (PlanError, ExportError) as error:
                raise HTTPException(error.status, error.code) from None
            except HTTPException:
                raise
            except ValueError:
                raise HTTPException(409, "EXPORT_SOURCE_CONFLICT") from None
            except Exception:
                raise HTTPException(503, "EXPORT_UNAVAILABLE") from None

        return guarded


def export_router(
    store: Callable[[], MatterStore], workflow: Callable[[], InvestigationWorkflow]
) -> APIRouter:
    router = APIRouter(
        prefix="/matters/{matter_id}/investigations/{thread_id}",
        route_class=ExportRoute,
    )

    def source(matter_id: str, thread_id: str) -> tuple[Matter, ExportService]:
        try:
            store().require_investigation_matter(matter_id, thread_id)
            matter = store().get_matter(matter_id)
        except MatterError:
            raise HTTPException(404, "EXPORT_NOT_FOUND") from None
        if matter is None:
            raise HTTPException(404, "EXPORT_NOT_FOUND")
        return matter, ExportService(DepositionService(workflow()))

    def response(download: Download) -> Response:
        return Response(
            download.content,
            media_type=DOCX_TYPE,
            headers={
                "Content-Disposition": f'attachment; filename="{download.filename}"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.get("/export.docx", response_class=Response)
    def investigation(
        matter_id: str,
        thread_id: str,
        principal: Annotated[OperatorPrincipal, Depends(require_operator)],
    ) -> Response:
        matter, service = source(matter_id, thread_id)
        return response(service.investigation(matter, thread_id))

    @router.get("/deposition-plans/{plan_id}/export.docx", response_class=Response)
    def deposition(
        matter_id: str,
        thread_id: str,
        plan_id: str,
        principal: Annotated[OperatorPrincipal, Depends(require_operator)],
        revision: Annotated[int | None, Query(ge=1, le=200)] = None,
    ) -> Response:
        matter, service = source(matter_id, thread_id)
        return response(service.deposition_plan(matter, thread_id, plan_id, revision))

    return router

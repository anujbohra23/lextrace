"""Read-only export operations inside the existing M7/M9 publication fence."""

from threading import BoundedSemaphore

from lextrace.deposition.service import DepositionService, PublishedContext
from lextrace.exports import render
from lextrace.exports.document import Download, ExportError
from lextrace.exports.render import ReportInput
from lextrace.investigations.contracts import InvestigationEvent
from lextrace.investigations.reviewed_chronology import build_reviewed_chronology
from lextrace.matter.contracts import Matter

EXPORT_SLOT = BoundedSemaphore(1)


class ExportService:
    def __init__(self, deposition: DepositionService) -> None:
        self.deposition = deposition

    @staticmethod
    def validate_context(context: PublishedContext) -> None:
        expected = build_reviewed_chronology(
            tuple(
                r
                for r in context.revisions
                if isinstance(r.original, InvestigationEvent)
            ),
            matter_id=context.snapshot.matter_id,
            require_accepted=True,
        )
        if (
            context.artifacts.reviewed_chronology != expected
            or context.artifacts.result is None
        ):
            raise ExportError("EXPORT_SOURCE_CONFLICT")

    def investigation(self, matter: Matter, thread: str) -> Download:
        if not EXPORT_SLOT.acquire(blocking=False):
            raise ExportError("EXPORT_BUSY", 503)
        try:
            with self.deposition.published(matter.matter_id, thread, recover=False) as (
                _,
                context,
            ):
                self.validate_context(context)
                return render.investigation(ReportInput(matter, context))
        finally:
            EXPORT_SLOT.release()

    def deposition_plan(
        self, matter: Matter, thread: str, plan_id: str, revision: int | None
    ) -> Download:
        if not EXPORT_SLOT.acquire(blocking=False):
            raise ExportError("EXPORT_BUSY", 503)
        try:
            with self.deposition.published(matter.matter_id, thread, recover=False) as (
                db,
                context,
            ):
                self.validate_context(context)
                view = self.deposition.approved_in_context(
                    db, context, plan_id, revision
                )
                latest = db.execute(
                    "SELECT MAX(revision) FROM deposition_revisions WHERE plan_id=?",
                    (plan_id,),
                ).fetchone()[0]
                if view.basis.outcome == "BLOCKED" or (
                    view.basis.outcome == "LIMITED"
                    and not view.current.acknowledges_limitations
                ):
                    raise ExportError("EXPORT_APPROVAL_REQUIRED")
                return render.deposition(ReportInput(matter, context, view, latest))
        finally:
            EXPORT_SLOT.release()

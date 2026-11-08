"""Synthetic, genuinely published M7 and approved M9 inputs for Word export."""

import pytest

from lextrace.deposition.contracts import PlanView, ReviewPlan
from lextrace.deposition.service import DepositionService
from lextrace.exports.render import ReportInput
from lextrace.exports.service import ExportService
from tests.unit.test_deposition import create
from tests.unit.test_investigation_durable import Harness


@pytest.fixture
def report_input(deposition: DepositionService, durable: Harness) -> ReportInput:
    thread = deposition.runtime.read("SELECT thread_id FROM investigation_runs")[0][0]
    matter = durable.runtime.store.get_matter(durable.runtime.matter)
    assert matter is not None
    with deposition.published(matter.matter_id, thread, recover=False) as (_, context):
        return ReportInput(matter, context)


@pytest.fixture
def approved_plan(deposition: DepositionService, durable: Harness) -> PlanView:
    view = create(deposition, durable)
    request = ReviewPlan(
        decision_id="approve-word-export",
        expected_revision=1,
        action="APPROVE",
        questions=view.current.questions,
        confirm=True,
        acknowledge_limitations=True,
    )
    return deposition.review(
        view.basis.matter_id,
        view.basis.thread_id,
        view.basis.plan_id,
        request,
        "export-reviewer",
    )


@pytest.fixture
def exporter(deposition: DepositionService) -> ExportService:
    return ExportService(deposition)

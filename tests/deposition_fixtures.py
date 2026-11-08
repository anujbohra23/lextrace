"""Offline M9 fixtures backed by genuine M7 SQLite publication."""

import pytest

from lextrace.deposition.service import DepositionService
from tests.unit.test_investigation_durable import Harness
from tests.unit.test_investigation_workflow import approval, linked


@pytest.fixture
def deposition(durable: Harness) -> DepositionService:
    first = durable.start()
    second = durable.workflow.resume(linked(durable.workflow, first))
    final = durable.workflow.resume(approval(second))
    assert final.stage == "APPROVED"
    return DepositionService(durable.workflow)

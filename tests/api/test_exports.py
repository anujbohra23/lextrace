# ruff: noqa: F811 -- shared offline fixtures
"""Authenticated binary downloads from real durable M7/M9 publications."""

from typing import NoReturn

import pytest
from fastapi.testclient import TestClient

from lextrace.deposition.contracts import PlanView, ReviewPlan
from lextrace.deposition.service import DepositionService
from lextrace.exports.document import DOCX_TYPE
from tests.api.test_investigations import client as client  # noqa: F401
from tests.deposition_fixtures import deposition as deposition  # noqa: F401
from tests.exports_fixtures import approved_plan as approved_plan  # noqa: F401
from tests.exports_fixtures import report_input as report_input  # noqa: F401
from tests.unit.test_docx_exports import paragraphs
from tests.unit.test_investigation_durable import Harness
from tests.unit.test_investigation_durable import durable as durable  # noqa: F401
from tests.unit.test_investigation_workflow import setup as memory_setup  # noqa: F401


def paths(service: DepositionService, plan: PlanView) -> tuple[str, str]:
    b = plan.basis
    base = f"/matters/{b.matter_id}/investigations/{b.thread_id}"
    return base + "/export.docx", base + f"/deposition-plans/{b.plan_id}/export.docx"


def test_both_authenticated_word_downloads(
    client: TestClient, deposition: DepositionService, approved_plan: PlanView
) -> None:
    for url in paths(deposition, approved_plan):
        response = client.get(url, headers={"Origin": "http://localhost:3000"})
        assert response.status_code == 200, (
            response.text[:100] if not response.content.startswith(b"PK") else ""
        )
        assert response.headers["content-type"] == DOCX_TYPE
        assert response.headers["content-disposition"].startswith(
            'attachment; filename="'
        )
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert (
            "Content-Disposition" in response.headers["access-control-expose-headers"]
        )
        assert response.content.startswith(b"PK")
        assert paragraphs(response.content)


@pytest.mark.parametrize("action", ["REVISE", "REJECT", "DEFER"])
def test_current_draft_rejected_or_deferred_cannot_export_implicitly(
    client: TestClient,
    deposition: DepositionService,
    approved_plan: PlanView,
    action: str,
) -> None:
    p = approved_plan
    request = ReviewPlan.model_validate(
        dict(
            decision_id="later-review",
            expected_revision=p.current.revision,
            action=action,
            questions=tuple(
                q.model_copy(update={"wording": "MUTABLE LATER WORDING?"})
                for q in p.current.questions
            ),
        )
    )
    deposition.review(
        p.basis.matter_id, p.basis.thread_id, p.basis.plan_id, request, "reviewer"
    )
    _, url = paths(deposition, p)
    assert client.get(url).status_code == 409
    assert client.get(url + "?revision=3").status_code == 404
    historical = client.get(url + "?revision=2")
    assert historical.status_code == 200
    text = "\n".join(paragraphs(historical.content))
    assert "MUTABLE LATER WORDING" not in text
    assert "Historical approved revision" in text
    assert "Approved revision: 2" in text


def test_unsigned_investigation_rejected_without_recovery_or_mutation(
    client: TestClient, durable: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = durable.start()
    before = durable.workflow.inspect(first.thread_id)

    def forbidden(thread: str) -> None:
        raise AssertionError("Export attempted workflow recovery")

    monkeypatch.setattr(durable.workflow, "reopen", forbidden)
    response = client.get(
        f"/matters/{first.matter_id}/investigations/{first.thread_id}/export.docx"
    )
    assert response.status_code == 409
    assert durable.workflow.inspect(first.thread_id) == before


@pytest.mark.parametrize("url_index", [0, 1])
def test_no_anonymous_or_cross_matter_download(
    client: TestClient,
    deposition: DepositionService,
    approved_plan: PlanView,
    durable: Harness,
    url_index: int,
) -> None:
    url = paths(deposition, approved_plan)[url_index]
    foreign = durable.runtime.store.create_matter("Other").matter_id
    assert (
        client.get(url.replace(approved_plan.basis.matter_id, foreign)).status_code
        == 404
    )
    assert (
        client.get(
            url.replace(approved_plan.basis.thread_id, "wrong-thread")
        ).status_code
        == 404
    )
    client.headers.pop("authorization")
    assert client.get(url).status_code == 401


@pytest.mark.parametrize("value", ["0", "-1", "bad", "201"])
def test_bad_revision_is_safe_validation_error(
    client: TestClient,
    deposition: DepositionService,
    approved_plan: PlanView,
    value: str,
) -> None:
    _, url = paths(deposition, approved_plan)
    response = client.get(url + "?revision=" + value)
    assert response.status_code == 422
    assert response.json() == {"detail": "EXPORT_INVALID_REQUEST"}


def test_storage_failures_never_disclose_private_paths(
    client: TestClient,
    deposition: DepositionService,
    approved_plan: PlanView,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(thread: str) -> NoReturn:
        raise RuntimeError("TOKEN SECRET /private/path")

    monkeypatch.setattr(deposition.workflow, "approved_snapshot", broken)
    for url in paths(deposition, approved_plan):
        response = client.get(url)
        assert response.status_code == 503
        assert response.json() == {"detail": "EXPORT_UNAVAILABLE"}


def test_export_size_failure_is_safe_json(
    client: TestClient,
    deposition: DepositionService,
    approved_plan: PlanView,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lextrace.exports import document

    monkeypatch.setattr(document, "MAX_TEXT_CHARACTERS", 1)
    for path in paths(deposition, approved_plan):
        response = client.get(path)
        assert response.status_code == 409
        assert response.json() == {"detail": "EXPORT_TOO_LARGE"}


def test_operator_binding_denies_export(
    client: TestClient,
    deposition: DepositionService,
    approved_plan: PlanView,
    durable: Harness,
) -> None:
    from lextrace.api.app import create_app
    from lextrace.config import AppSettings

    # Bind the existing workspace to the authenticated pilot operator first.
    assert client.get(paths(deposition, approved_plan)[0]).status_code == 200
    settings = AppSettings.from_environment().model_copy(
        update={"operator_id": "other-operator"}
    )
    with TestClient(
        create_app(settings=settings, investigation_workflow=durable.workflow)
    ) as foreign:
        foreign.headers.update(client.headers)
        for path in paths(deposition, approved_plan):
            assert foreign.get(path).status_code == 403


def test_plan_cannot_be_exported_through_another_signed_thread(
    client: TestClient,
    deposition: DepositionService,
    approved_plan: PlanView,
    durable: Harness,
) -> None:
    from tests.unit.test_investigation_workflow import approval, linked

    first = durable.start()
    gate = durable.workflow.resume(linked(durable.workflow, first))
    assert durable.workflow.resume(approval(gate)).stage == "APPROVED"
    _, path = paths(deposition, approved_plan)
    wrong = path.replace(approved_plan.basis.thread_id, first.thread_id)
    assert client.get(wrong + "?revision=2").status_code == 404


def test_tampered_approved_revision_is_not_downloaded(
    client: TestClient, deposition: DepositionService, approved_plan: PlanView
) -> None:
    b = approved_plan.basis
    with deposition.runtime.transaction() as db:
        db.execute(
            "UPDATE deposition_revisions SET payload='{}' "
            "WHERE plan_id=? AND revision=2",
            (b.plan_id,),
        )
    _, path = paths(deposition, approved_plan)
    assert client.get(path).status_code == 409


def test_export_busy_returns_safe_service_error(
    client: TestClient, deposition: DepositionService, approved_plan: PlanView
) -> None:
    from lextrace.exports.service import EXPORT_SLOT

    assert EXPORT_SLOT.acquire(blocking=False)
    try:
        response = client.get(paths(deposition, approved_plan)[0])
        assert response.status_code == 503
        assert response.json() == {"detail": "EXPORT_BUSY"}
    finally:
        EXPORT_SLOT.release()

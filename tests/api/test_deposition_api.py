# ruff: noqa: F811 -- shared offline fixtures
"""M9 API uses real M7 publication and domain transactions with synthetic services."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from lextrace.deposition.service import DepositionService
from tests.api.test_investigations import client as client  # noqa: F401
from tests.deposition_fixtures import deposition as deposition  # noqa: F401
from tests.unit.test_investigation_durable import Harness
from tests.unit.test_investigation_durable import durable as durable  # noqa: F401
from tests.unit.test_investigation_workflow import setup as memory_setup  # noqa: F401


def location(service: DepositionService, harness: Harness) -> tuple[str, str]:
    thread = service.runtime.read("SELECT thread_id FROM investigation_runs")[0][0]
    signed = service.workflow.approved_snapshot(thread)
    return (
        f"/matters/{harness.runtime.matter}/investigations/{thread}/deposition-plans",
        signed.snapshot_id,
    )


def make(
    client: TestClient, service: DepositionService, harness: Harness
) -> tuple[str, dict[str, Any]]:
    base, snapshot = location(service, harness)
    response = client.post(
        base, json={"snapshot_id": snapshot, "request_id": "create-api"}
    )
    assert response.status_code == 201, response.text
    view = response.json()
    return base + "/" + view["basis"]["plan_id"], view


def command(
    view: dict[str, Any], action: str = "REVISE", decision_id: str = "edit-1"
) -> dict[str, Any]:
    return {
        "review": {
            "decision_id": decision_id,
            "expected_revision": view["current"]["revision"],
            "action": action,
            "questions": view["current"]["questions"],
            "confirm": True,
            "acknowledge_limitations": True,
        }
    }


def test_create_edit_approve_history(
    client: TestClient, deposition: DepositionService, durable: Harness
) -> None:
    path, view = make(client, deposition, durable)
    base, snapshot = location(deposition, durable)
    assert (
        client.post(
            base, json={"snapshot_id": snapshot, "request_id": "create-api"}
        ).json()
        == view
    )
    assert len(client.get(base).json()) == 1
    assert client.get(path).json() == view
    assert len(client.get(path + "/witnesses").json()) == 2
    request = command(view)
    request["review"]["questions"][0].update(
        wording="Could you explain the basis for this account?",
        rationale="Clarify the quoted source.",
        group="Foundation interview",
        order=99,
    )
    updated = client.post(path + "/review", json=request)
    assert updated.status_code == 200, updated.text
    assert updated.json()["current"]["revision"] == 2
    assert updated.json()["current"]["reviewer_id"] == "test-operator"
    assert client.post(path + "/review", json=request).json() == updated.json()
    assert (
        client.post(
            path + "/review", json=command(view, decision_id="stale-browser")
        ).status_code
        == 409
    )
    approve = command(updated.json(), "APPROVE", "approve-2")
    approve["review"]["questions"][0]["disposition"] = "DEFER"
    final = client.post(path + "/review", json=approve)
    assert final.status_code == 200, final.text
    assert final.json()["current"]["status"] == "APPROVED"
    approved = client.get(path + "/approved/3")
    assert approved.status_code == 200
    assert all(
        q["disposition"] == "INCLUDE" for q in approved.json()["current"]["questions"]
    )
    assert len(client.get(path + "/history").json()) == 3
    assert client.get(path + "/approved/1").status_code == 404


@pytest.mark.parametrize(
    "action,status", [("REJECT", "REJECTED"), ("DEFER", "DEFERRED")]
)
def test_reject_and_defer(
    client: TestClient,
    deposition: DepositionService,
    durable: Harness,
    action: str,
    status: str,
) -> None:
    path, view = make(client, deposition, durable)
    response = client.post(path + "/review", json=command(view, action))
    assert response.status_code == 200
    assert response.json()["current"]["status"] == status
    assert client.get(path + "/approved/2").status_code == 404


@pytest.mark.parametrize(
    "field,value",
    [
        ("evidence_ids", ["f" * 64]),
        ("witness_id", "f" * 64),
        ("wording", "Unsupported declarative assertion"),
        ("category", "FOLLOW_UP"),
    ],
)
def test_edits_cannot_replace_evidence_or_identity(
    client: TestClient,
    deposition: DepositionService,
    durable: Harness,
    field: str,
    value: object,
) -> None:
    path, view = make(client, deposition, durable)
    request = command(view)
    request["review"]["questions"][0][field] = value
    response = client.post(path + "/review", json=request)
    assert response.status_code == 422, response.text
    assert client.get(path).json()["current"]["revision"] == 1


def test_retry_conflict_and_approval_confirmation(
    client: TestClient, deposition: DepositionService, durable: Harness
) -> None:
    path, view = make(client, deposition, durable)
    request = command(view, "APPROVE")
    request["review"]["confirm"] = False
    assert client.post(path + "/review", json=request).status_code == 422
    request["review"]["confirm"] = True
    assert client.post(path + "/review", json=request).status_code == 200
    request["review"]["action"] = "REJECT"
    assert client.post(path + "/review", json=request).status_code == 409


@pytest.mark.parametrize(
    "suffix", ["", "/witnesses", "/history", "/approved/2", "/review"]
)
def test_wrong_thread_or_matter_never_authorizes_plan(
    client: TestClient, deposition: DepositionService, durable: Harness, suffix: str
) -> None:
    path, view = make(client, deposition, durable)
    foreign = durable.runtime.store.create_matter("Foreign").matter_id
    for wrong in (
        path.replace(durable.runtime.matter, foreign),
        path.replace(view["basis"]["thread_id"], "wrong"),
    ):
        response = (
            client.post(wrong + suffix, json=command(view))
            if suffix == "/review"
            else client.get(wrong + suffix)
        )
        assert response.status_code == 404


def test_unsigned_snapshot_and_forged_identity(
    client: TestClient, durable: Harness
) -> None:
    first = durable.start()
    path = (
        f"/matters/{first.matter_id}/investigations/{first.thread_id}/deposition-plans"
    )
    assert (
        client.post(
            path, json={"snapshot_id": "f" * 64, "request_id": "request"}
        ).status_code
        == 409
    )
    assert (
        client.post(
            path,
            json={
                "snapshot_id": "f" * 64,
                "request_id": "request",
                "reviewer_id": "forged",
            },
        ).status_code
        == 422
    )


def test_authentication_and_safe_failures(
    client: TestClient,
    deposition: DepositionService,
    durable: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, view = make(client, deposition, durable)
    client.headers.pop("authorization")
    assert client.get(path).status_code == 401
    client.headers["authorization"] = (
        "Bearer test-only-credential-00000000000000000000000000"
    )
    request = command(view)
    request["review"]["reviewer_id"] = "forged"
    assert client.post(path + "/review", json=request).status_code == 422

    def broken(thread: str) -> None:
        raise RuntimeError("SECRET /private/runtime/path")

    monkeypatch.setattr(durable.workflow, "reopen", broken)
    response = client.get(path)
    assert response.status_code == 503
    assert response.json() == {"detail": "DEPOSITION_UNAVAILABLE"}
    assert "SECRET" not in response.text


@pytest.mark.parametrize("mode", ["blocked", "partial"])
def test_limited_and_blocked_published_inputs(
    client: TestClient, durable: Harness, mode: str
) -> None:
    from dataclasses import replace

    from lextrace.investigations.contracts import EntityMention
    from tests.unit.test_investigation_workflow import approval, linked

    if mode == "blocked":
        durable.services.verified = tuple(
            replace(
                v,
                valid_candidates=tuple(
                    c for c in v.valid_candidates if isinstance(c, EntityMention)
                ),
            )
            for v in durable.services.verified
        )
    else:
        durable.services.verified = tuple(
            replace(v, coverage=replace(v.coverage, status="PARTIAL"))
            for v in durable.services.verified
        )
    first = durable.start()
    second = durable.workflow.resume(
        linked(durable.workflow, first).model_copy(
            update={"acknowledge_incomplete_coverage": True}
        )
    )
    final = durable.workflow.resume(
        approval(second, acknowledge_incomplete_coverage=True)
    )
    assert final.stage == "APPROVED"
    service = DepositionService(durable.workflow)
    path, view = make(client, service, durable)
    assert view["basis"]["outcome"] == ("BLOCKED" if mode == "blocked" else "LIMITED")
    request = command(view, "APPROVE")
    request["review"]["acknowledge_limitations"] = False
    assert client.post(path + "/review", json=request).status_code == 422
    if mode == "partial":
        request["review"]["acknowledge_limitations"] = True
        assert client.post(path + "/review", json=request).status_code == 200


def test_other_signed_investigation_cannot_read_plan(
    client: TestClient, deposition: DepositionService, durable: Harness
) -> None:
    from tests.unit.test_investigation_workflow import approval, linked

    path, view = make(client, deposition, durable)
    first = durable.start()
    second = durable.workflow.resume(linked(durable.workflow, first))
    assert durable.workflow.resume(approval(second)).stage == "APPROVED"
    wrong = path.replace(view["basis"]["thread_id"], first.thread_id)
    for suffix in ("", "/witnesses", "/history", "/approved/2"):
        assert client.get(wrong + suffix).status_code == 404
    assert client.post(wrong + "/review", json=command(view)).status_code == 404


def test_workspace_operator_binding_still_denies_foreign_operator(
    client: TestClient, deposition: DepositionService, durable: Harness
) -> None:
    from lextrace.api.app import create_app
    from lextrace.config import AppSettings

    path, _ = make(client, deposition, durable)
    settings = AppSettings.from_environment().model_copy(
        update={"operator_id": "foreign-operator"}
    )
    with TestClient(
        create_app(settings=settings, investigation_workflow=durable.workflow)
    ) as foreign:
        foreign.headers.update(client.headers)
        assert foreign.get(path).status_code == 403

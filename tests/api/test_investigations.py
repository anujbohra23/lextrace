# ruff: noqa: F811 -- shared durable synthetic fixtures
"""Authenticated M8 adapters over real durable M7 checkpoints, offline."""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lextrace.api.app import create_app
from lextrace.config import AppSettings
from lextrace.investigations.workflow import HumanApproval
from tests.unit.test_investigation_durable import Crash, Harness
from tests.unit.test_investigation_durable import durable as durable  # noqa: F401
from tests.unit.test_investigation_workflow import linked
from tests.unit.test_investigation_workflow import setup as memory_setup  # noqa: F401


@pytest.fixture
def client(durable: Harness, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("LEXTRACE_MATTER_DB", str(durable.root / "db"))
    monkeypatch.setenv("LEXTRACE_PRIVATE_MATTER_ROOT", str(durable.root / "private"))
    monkeypatch.setenv("LEXTRACE_ADOPT_LEGACY_MATTERS", "true")
    with TestClient(create_app(investigation_workflow=durable.workflow)) as client:
        client.headers["authorization"] = (
            "Bearer test-only-credential-00000000000000000000000000"
        )
        yield client


def base(durable: Harness) -> str:
    return f"/matters/{durable.runtime.matter}/investigations"


def start(client: TestClient, durable: Harness) -> dict[str, Any]:
    response = client.post(
        base(durable),
        json={
            "approved_versions": [
                v.model_dump(mode="json") for v in durable.runtime.approved
            ],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


def decision(state: dict[str, Any]) -> dict[str, Any]:
    return {k: state[k] for k in ("run_id", "token", "digest")} | {
        "gate": state["checkpoint"],
        "decision": "APPROVE",
    }


def test_happy_path(client: TestClient, durable: Harness) -> None:
    first = start(client, durable)
    url = f"{base(durable)}/{first['thread_id']}"
    assert first["checkpoint"] == "evidence"
    assert first["review"]["records"]
    assert client.get(url).json() == first
    assert client.get(base(durable)).json() == [first]
    request = decision(first)
    request["entity_links"] = [
        v.model_dump(mode="json")
        for v in linked(
            durable.workflow, durable.workflow.inspect(first["thread_id"])
        ).entity_links
    ]
    second = client.post(url + "/decisions", json=request)
    assert second.status_code == 200, second.text
    assert second.json()["checkpoint"] == "findings"
    assert second.json()["review"]["findings"]
    assert client.post(url + "/decisions", json=request).json() == second.json()
    final = client.post(url + "/decisions", json=decision(second.json()))
    assert final.status_code == 200, final.text
    assert final.json()["stage"] == "APPROVED"
    path = url + "/snapshots/" + final.json()["snapshot_id"]
    snapshot = client.get(path)
    assert snapshot.status_code == 200
    assert snapshot.json()["reviewer_id"] == "test-operator"
    for identity in [snapshot.json()["chronology_id"], *snapshot.json()["finding_ids"]]:
        assert client.get(path + "/artifacts/" + identity).status_code == 200
    assert client.get(path + "/artifacts/missing").status_code == 404
    assert client.get(url + "/snapshots/wrong").status_code == 404
    assert (
        client.post(url + "/decisions", json=decision(second.json())).json()
        == final.json()
    )


@pytest.mark.parametrize("gate", ["evidence", "findings"])
def test_rejection(client: TestClient, durable: Harness, gate: str) -> None:
    state = start(client, durable)
    url = f"{base(durable)}/{state['thread_id']}"
    if gate == "findings":
        state = client.post(url + "/decisions", json=decision(state)).json()
    request = decision(state) | {"decision": "REJECT"}
    response = client.post(url + "/decisions", json=request)
    assert response.status_code == 200, response.text
    assert response.json()["stage"] == "BLOCKED"
    assert response.json()["errors"] == ["REJECTED"]
    assert client.post(url + "/decisions", json=request).json() == response.json()
    assert client.get(url + "/snapshots/missing").status_code == 409


@pytest.mark.parametrize("field", ["token", "digest", "run_id", "gate"])
def test_invalid_decision(client: TestClient, durable: Harness, field: str) -> None:
    state = start(client, durable)
    request = decision(state)
    request[field] = "findings" if field == "gate" else "0" * 64
    url = f"{base(durable)}/{state['thread_id']}"
    assert client.post(url + "/decisions", json=request).status_code == 409
    assert client.get(url).json() == state


@pytest.mark.parametrize("scope", ["missing", "foreign", "thread"])
def test_ownership(client: TestClient, durable: Harness, scope: str) -> None:
    state = start(client, durable)
    matter = durable.runtime.matter
    thread = state["thread_id"]
    if scope == "missing":
        matter = "missing"
    elif scope == "foreign":
        matter = durable.runtime.store.create_matter("foreign").matter_id
    else:
        thread = "wrong"
    url = f"/matters/{matter}/investigations/{thread}"
    assert client.get(url).status_code == 404
    assert client.post(url + "/decisions", json=decision(state)).status_code == 404
    assert client.get(url + "/snapshots/id").status_code == 404
    assert client.get(url + "/snapshots/id/artifacts/id").status_code == 404
    if scope == "missing":
        assert client.get(f"/matters/{matter}/investigations").status_code == 404
        assert (
            client.post(
                f"/matters/{matter}/investigations",
                json={
                    "approved_versions": [
                        v.model_dump(mode="json") for v in durable.runtime.approved
                    ],
                },
            ).status_code
            == 404
        )


@pytest.mark.parametrize("nested", [False, True])
def test_reviewer_not_accepted(
    client: TestClient, durable: Harness, nested: bool
) -> None:
    state = start(client, durable)
    request = decision(state)
    if nested:
        request["evidence_decisions"] = [{"reviewer_id": "attacker"}]
    else:
        request["reviewer_id"] = "attacker"
    assert (
        client.post(
            f"{base(durable)}/{state['thread_id']}/decisions", json=request
        ).status_code
        == 422
    )


@pytest.mark.parametrize(
    "boundary", ["after_decision_commit", "after_publication_commit"]
)
def test_restart(client: TestClient, durable: Harness, boundary: str) -> None:
    state = start(client, durable)
    url = f"{base(durable)}/{state['thread_id']}"
    if boundary == "after_publication_commit":
        state = client.post(url + "/decisions", json=decision(state)).json()
    request = decision(state)
    durable.inject(boundary)
    with pytest.raises(Crash):
        durable.workflow.resume(
            HumanApproval.model_validate(
                {
                    **request,
                    "matter_id": durable.runtime.matter,
                    "thread_id": state["thread_id"],
                    "reviewer_id": "test-operator",
                }
            )
        )
    workflow = durable.restart()
    with TestClient(
        create_app(
            settings=AppSettings.from_environment(), investigation_workflow=workflow
        )
    ) as restarted:
        restarted.headers.update(client.headers)
        recovered = restarted.get(url)
        assert recovered.status_code == 200, recovered.text
        expected = (
            "APPROVED" if boundary == "after_publication_commit" else "WAITING_REVIEW"
        )
        assert recovered.json()["stage"] == expected
        assert (
            restarted.post(url + "/decisions", json=request).json() == recovered.json()
        )
        if boundary == "after_publication_commit":
            assert workflow.runtime is not None
            assert (
                workflow.runtime.read("SELECT COUNT(*) FROM investigation_snapshots")[
                    0
                ][0]
                == 1
            )


def test_auth_required(client: TestClient, durable: Harness) -> None:
    client.headers.pop("authorization")
    assert client.get(base(durable)).status_code == 401


def test_evidence_decision_identity_and_conflict(
    client: TestClient, durable: Harness
) -> None:
    state = start(client, durable)
    url = f"{base(durable)}/{state['thread_id']}"
    from tests.unit.test_investigation_review import command

    revision = durable.services.revisions(
        durable.workflow.artifacts[state["thread_id"]]
    )[0]
    item = command(
        revision.original,
        expected_revision=revision.revision,
        decision_id="api-review",
        reviewer_id="ignored",
    )
    payload = item.model_dump(mode="json", exclude={"reviewer_id"})
    request = decision(state) | {"evidence_decisions": [payload]}
    response = client.post(url + "/decisions", json=request)
    assert response.status_code == 200, response.text
    reviewed = durable.runtime.repo.current_review(item.target)
    assert reviewed and reviewed.decision
    assert reviewed.decision.reviewer_id == "test-operator"
    assert client.post(url + "/decisions", json=request).json() == response.json()
    assert (
        len(durable.runtime.repo.review_history(item.target)) == revision.revision + 1
    )
    conflict = request | {"decision": "REJECT"}
    assert client.post(url + "/decisions", json=conflict).status_code == 409
    final = client.post(url + "/decisions", json=decision(response.json()))
    assert final.status_code == 200
    assert client.post(url + "/decisions", json=request).json() == final.json()


@pytest.mark.parametrize("error", [ValueError, RuntimeError])
def test_safe_errors(
    client: TestClient,
    durable: Harness,
    monkeypatch: pytest.MonkeyPatch,
    error: type[Exception],
) -> None:
    state = start(client, durable)

    def broken(thread_id: str) -> None:
        raise error("/private/secret/path TOKEN checkpoint internals")

    monkeypatch.setattr(durable.workflow, "reopen", broken)
    response = client.get(f"{base(durable)}/{state['thread_id']}")
    assert response.status_code == (409 if error is ValueError else 503)
    assert "secret" not in response.text
    assert "TOKEN" not in response.text
    assert "checkpoint" not in response.text


def test_stale_inputs(client: TestClient, durable: Harness) -> None:
    state = start(client, durable)
    source = durable.workflow.inspect(state["thread_id"]).sources[0]
    (
        durable.root
        / "private"
        / source.matter_id
        / source.document_id
        / "extracted.txt"
    ).write_text("changed")
    url = f"{base(durable)}/{state['thread_id']}"
    assert client.get(url).status_code == 409
    assert client.post(url + "/decisions", json=decision(state)).status_code == 409


def test_invalid_scope(client: TestClient, durable: Harness) -> None:
    response = client.post(
        base(durable),
        json={
            "approved_versions": [{"document_id": "wrong", "version_id": "wrong"}],
        },
    )
    assert response.status_code == 409
    assert client.get(base(durable)).json() == []


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("kind", ["chronology", "finding"])
def test_published_artifacts_ignore_replaced_working_copy(
    client: TestClient,
    durable: Harness,
    monkeypatch: pytest.MonkeyPatch,
    restart: bool,
    kind: str,
) -> None:
    from dataclasses import replace

    from lextrace.investigations.workflow import (
        ApprovedInvestigationSnapshot,
        InvestigationWorkflow,
    )

    state = start(client, durable)
    thread = state["thread_id"]
    url = f"{base(durable)}/{thread}"
    request = decision(state) | {
        "entity_links": [
            v.model_dump(mode="json")
            for v in linked(
                durable.workflow, durable.workflow.inspect(thread)
            ).entity_links
        ],
    }
    second = client.post(url + "/decisions", json=request)
    assert second.status_code == 200, second.text
    final = client.post(url + "/decisions", json=decision(second.json()))
    assert final.status_code == 200, final.text
    path = url + "/snapshots/" + final.json()["snapshot_id"]
    signed = client.get(path).json()
    identity = (
        signed["chronology_id"] if kind == "chronology" else signed["finding_ids"][0]
    )
    artifact_path = path + "/artifacts/" + identity
    original = client.get(artifact_path)
    assert original.status_code == 200

    def check(active: TestClient, workflow: InvestigationWorkflow) -> None:
        read_snapshot = workflow.approved_snapshot

        def replaced(thread_id: str) -> ApprovedInvestigationSnapshot:
            published = read_snapshot(thread_id)
            working = workflow.artifacts[thread_id]
            if kind == "chronology":
                assert working.reviewed_chronology is not None
                changed = replace(
                    working.reviewed_chronology, ordering_policy="UNSIGNED"
                )
                working.reviewed_chronology = changed
            else:
                assert working.result is not None
                findings = tuple(
                    f.model_copy(
                        update={
                            "verification": "UNSIGNED",
                        }
                    )
                    for f in working.result.findings
                )
                working.result = working.result.model_copy(
                    update={"findings": findings}
                )
            # Replace the whole cache entry after snapshot validation, too.
            workflow.artifacts[thread_id] = replace(working)
            return published

        monkeypatch.setattr(workflow, "approved_snapshot", replaced)
        response = active.get(artifact_path)
        assert response.status_code == 200, response.text
        assert response.json() == original.json()
        assert "UNSIGNED" not in response.text

    if restart:
        workflow = durable.restart()
        with TestClient(create_app(investigation_workflow=workflow)) as restarted:
            restarted.headers.update(client.headers)
            check(restarted, workflow)
    else:
        check(client, durable.workflow)


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_signed_journal_entry_required(
    client: TestClient,
    durable: Harness,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    from lextrace.investigations.workflow import ApprovedInvestigationSnapshot

    state = start(client, durable)
    url = f"{base(durable)}/{state['thread_id']}"
    second = client.post(url + "/decisions", json=decision(state)).json()
    final = client.post(url + "/decisions", json=decision(second)).json()
    path = url + "/snapshots/" + final["snapshot_id"]
    signed = client.get(path).json()
    read_snapshot = durable.workflow.approved_snapshot

    def damaged(thread_id: str) -> ApprovedInvestigationSnapshot:
        published = read_snapshot(thread_id)
        runtime = durable.workflow.runtime
        assert runtime is not None
        with runtime.transaction() as db:
            if damage == "missing":
                db.execute(
                    "DELETE FROM investigation_artifacts "
                    "WHERE thread_id=? AND digest=?",
                    (thread_id, published.artifact_digest),
                )
            else:
                db.execute(
                    "UPDATE investigation_artifacts SET payload=x'00' "
                    "WHERE thread_id=? AND digest=?",
                    (thread_id, published.artifact_digest),
                )
        return published

    monkeypatch.setattr(durable.workflow, "approved_snapshot", damaged)
    response = client.get(path + "/artifacts/" + signed["chronology_id"])
    assert response.status_code == 409
    assert response.json() == {"detail": "INVESTIGATION_STATE_CONFLICT"}


@pytest.mark.parametrize(
    "field,value",
    [
        ("digest", "malformed"),
        ("run_id", ""),
        ("token", ""),
    ],
)
def test_invalid_approval_contract(
    client: TestClient,
    durable: Harness,
    field: str,
    value: str,
) -> None:
    state = start(client, durable)
    url = f"{base(durable)}/{state['thread_id']}"
    response = client.post(url + "/decisions", json=decision(state) | {field: value})
    assert response.status_code == 422
    assert response.json() == {"detail": "INVESTIGATION_INVALID_REQUEST"}
    assert client.get(url).json() == state


@pytest.mark.parametrize(
    "field,value",
    [
        ("decision_id", ""),
        ("expected_revision", 0),
        ("timestamp", "2020-01-01T01:00:00+01:00"),
        ("decision", "EDIT"),
        ("resolved_entity_id", "hidden-edit"),
    ],
)
def test_invalid_nested_review_contract(
    client: TestClient,
    durable: Harness,
    field: str,
    value: str | int,
) -> None:
    from tests.unit.test_investigation_review import command

    state = start(client, durable)
    revision = durable.services.revisions(
        durable.workflow.artifacts[state["thread_id"]]
    )[0]
    item = command(
        revision.original,
        expected_revision=revision.revision,
        decision_id="invalid-api-review",
    )
    payload = item.model_dump(mode="json", exclude={"reviewer_id"}) | {field: value}
    url = f"{base(durable)}/{state['thread_id']}"
    response = client.post(
        url + "/decisions",
        json=decision(state)
        | {
            "evidence_decisions": [payload],
        },
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "INVESTIGATION_INVALID_REQUEST"}
    assert client.get(url).json() == state
    assert len(durable.runtime.repo.review_history(item.target)) == revision.revision


@pytest.mark.parametrize("error", ["runtime", "storage", "validation"])
def test_workflow_failures_are_not_client_validation(
    client: TestClient,
    durable: Harness,
    monkeypatch: pytest.MonkeyPatch,
    error: str,
) -> None:
    import sqlite3

    from pydantic import ValidationError

    state = start(client, durable)
    failure = (
        ValidationError.from_exception_data("InternalWorkflowContract", [])
        if error == "validation"
        else sqlite3.OperationalError("private storage failure")
        if error == "storage"
        else RuntimeError("private runtime failure")
    )

    def broken(approval: HumanApproval) -> None:
        raise failure

    monkeypatch.setattr(durable.workflow, "resume", broken)
    response = client.post(
        f"{base(durable)}/{state['thread_id']}/decisions", json=decision(state)
    )
    assert response.status_code == (409 if error == "validation" else 503)
    assert response.json()["detail"] != "INVESTIGATION_INVALID_REQUEST"
    assert "private" not in response.text


def test_eligible_versions_can_start(client: TestClient, durable: Harness) -> None:
    response = client.get(base(durable) + "/eligible-versions")
    assert response.status_code == 200, response.text
    versions = response.json()
    expected = sorted(
        (v.model_dump(mode="json") for v in durable.runtime.approved),
        key=lambda v: v["document_id"],
    )
    assert versions == expected
    assert all(set(v) == {"document_id", "version_id"} for v in versions)
    assert response.headers["cache-control"] == "no-store"
    started = client.post(base(durable), json={"approved_versions": versions})
    assert started.status_code == 201, started.text
    assert started.json()["checkpoint"] == "evidence"


@pytest.mark.parametrize("matter_id", ["missing", "0" * 32])
def test_eligible_versions_missing_matter(client: TestClient, matter_id: str) -> None:
    response = client.get(f"/matters/{matter_id}/investigations/eligible-versions")
    assert response.status_code == 404
    assert response.json() == {"detail": "INVESTIGATION_NOT_FOUND"}


def test_eligible_versions_empty_matter(client: TestClient, durable: Harness) -> None:
    empty = durable.runtime.store.create_matter("Empty")
    response = client.get(
        f"/matters/{empty.matter_id}/investigations/eligible-versions"
    )
    assert response.status_code == 200
    assert response.json() == []


def test_eligibility_uses_readiness_and_active_manifests(
    client: TestClient, durable: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lextrace.matter.documents import ExtractedDocument

    store = durable.runtime.store
    repo = durable.runtime.repo
    matter_id = durable.runtime.matter
    ready = store.add_document(matter_id, "ready.txt", b"Ready source")
    partial = repo.active_document_version(matter_id, ready.document_id)
    assert partial is not None
    repo.create_document_version(
        partial.model_copy(
            update={
                "version_id": "partial-active",
                "coverage": "PARTIAL",
            }
        )
    )
    legacy = store.add_document(matter_id, "legacy.txt", b"Missing manifest")
    deleted = store.add_document(matter_id, "deleted.txt", b"Deleted source")
    store.delete_document(matter_id, deleted.document_id)
    foreign_matter = store.create_matter("Foreign")
    foreign = store.add_document(
        foreign_matter.matter_id, "foreign.txt", b"Foreign source"
    )
    with repo._db:
        repo._db.execute(
            "DELETE FROM investigation_document_versions WHERE document_id=?",
            (legacy.document_id,),
        )
    with monkeypatch.context() as patch:
        patch.setattr(
            "lextrace.matter.store.extract_document",
            lambda *_: ExtractedDocument(
                "",
                (0,),
                1,
                ocr_required=True,
            ),
        )
        ocr = store.add_document(matter_id, "scan.pdf", b"Synthetic scanned file")
    assert ocr.ingestion_status == "OCR_REQUIRED"
    response = client.get(base(durable) + "/eligible-versions")
    assert response.status_code == 200, response.text
    pairs = response.json()
    assert {"document_id": ready.document_id, "version_id": "partial-active"} in pairs
    for excluded in (legacy, deleted, foreign, ocr):
        assert not any(v["document_id"] == excluded.document_id for v in pairs)
    # Listing never repairs legacy manifests or infers a human approval flag.
    assert repo.list_document_versions(matter_id, legacy.document_id) == []


@pytest.mark.parametrize("invalid", ["retired", "unknown", "foreign"])
def test_start_revalidates_listed_versions(
    client: TestClient, durable: Harness, invalid: str
) -> None:
    listed = client.get(base(durable) + "/eligible-versions").json()
    selected = listed[0]
    repo = durable.runtime.repo
    current = repo.active_document_version(
        durable.runtime.matter, selected["document_id"]
    )
    assert current is not None
    if invalid == "retired":
        repo.create_document_version(
            current.model_copy(update={"version_id": "replacement"})
        )
        refreshed = client.get(base(durable) + "/eligible-versions").json()
        assert selected not in refreshed
        assert {
            "document_id": selected["document_id"],
            "version_id": "replacement",
        } in refreshed
    elif invalid == "unknown":
        selected = {**selected, "version_id": "unknown"}
    else:
        foreign = durable.runtime.store.create_matter("Other matter")
        doc = durable.runtime.store.add_document(
            foreign.matter_id, "foreign.txt", b"Foreign"
        )
        version = repo.active_document_version(foreign.matter_id, doc.document_id)
        assert version is not None
        selected = {"document_id": doc.document_id, "version_id": version.version_id}
    response = client.post(base(durable), json={"approved_versions": [selected]})
    assert response.status_code == 409
    assert response.json() == {"detail": "INVESTIGATION_STATE_CONFLICT"}
    assert repo.list_threads(durable.runtime.matter) == ()


def test_eligible_versions_without_workflow_or_model(
    client: TestClient, durable: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LEXTRACE_LLM_MODEL", raising=False)
    with TestClient(create_app()) as fresh:
        fresh.headers.update(client.headers)
        response = fresh.get(base(durable) + "/eligible-versions")
        assert response.status_code == 200, response.text
        assert response.json()
        # Merely listing sources must not create workflow checkpoints or runs.
        assert durable.runtime.repo.list_threads(durable.runtime.matter) == ()


@pytest.mark.parametrize("credential", [None, "wrong-credential"])
def test_eligible_versions_require_auth(
    client: TestClient, durable: Harness, credential: str | None
) -> None:
    client.headers.pop("authorization")
    if credential:
        client.headers["authorization"] = f"Bearer {credential}"
    response = client.get(base(durable) + "/eligible-versions")
    assert response.status_code == 401
    assert "document_id" not in response.text

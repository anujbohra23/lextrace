# ruff: noqa: F811 -- shared synthetic durable fixture
"""Durable start request identity, distinct from source or thread possession."""

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.api.test_investigations import base, client, decision  # noqa: F401
from tests.unit.test_investigation_durable import Harness, durable  # noqa: F401
from tests.unit.test_investigation_workflow import setup as memory_setup  # noqa: F401


def payload(
    durable: Harness, request_id: str | None = "stable-start"
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "approved_versions": [
            v.model_dump(mode="json") for v in durable.runtime.approved
        ]
    }
    if request_id is not None:
        body["request_id"] = request_id
    return body


def test_repeat_and_conflicting_start_identity(
    client: TestClient, durable: Harness
) -> None:
    body = payload(durable)
    first = client.post(base(durable), json=body)
    assert first.status_code == 201
    assert client.post(base(durable), json=body).json() == first.json()
    assert len(durable.runtime.repo.list_threads(durable.runtime.matter)) == 1
    changed = body | {"budgets": {"model_calls": 1}}
    assert client.post(base(durable), json=changed).status_code == 409
    assert (
        client.post(base(durable), json=body | {"request_id": "distinct"}).json()[
            "thread_id"
        ]
        != first.json()["thread_id"]
    )


def test_legacy_starts_stay_distinct(client: TestClient, durable: Harness) -> None:
    first = client.post(base(durable), json=payload(durable, None)).json()
    second = client.post(base(durable), json=payload(durable, None)).json()
    assert first["thread_id"] != second["thread_id"]


def test_same_instance_concurrent_start(client: TestClient, durable: Harness) -> None:
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda _: client.post(base(durable), json=payload(durable)), range(2)
            )
        )
    assert [r.status_code for r in responses] == [201, 201]
    assert responses[0].json()["thread_id"] == responses[1].json()["thread_id"]
    assert durable.extraction_calls == 1


def test_start_identity_is_not_an_ownership_bypass(
    client: TestClient, durable: Harness
) -> None:
    first = client.post(base(durable), json=payload(durable))
    assert first.status_code == 201
    other = durable.runtime.store.create_matter("TEST-ONLY other matter").matter_id
    assert (
        client.post(
            f"/matters/{other}/investigations", json=payload(durable)
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/matters/missing/investigations", json=payload(durable)
        ).status_code
        == 404
    )
    assert (
        client.post(
            base(durable), json=payload(durable) | {"operator_id": "other"}
        ).status_code
        == 422
    )
    client.headers.pop("authorization")
    assert client.post(base(durable), json=payload(durable)).status_code == 401


def test_retry_after_review_returns_current_run(
    client: TestClient, durable: Harness
) -> None:
    body = payload(durable)
    first = client.post(base(durable), json=body).json()
    second = client.post(
        base(durable) + "/" + first["thread_id"] + "/decisions", json=decision(first)
    )
    assert second.status_code == 200
    assert client.post(base(durable), json=body).json() == second.json()


def test_key_is_scoped_to_matter_not_global(
    client: TestClient, durable: Harness
) -> None:
    first = client.post(base(durable), json=payload(durable)).json()
    other = durable.runtime.store.create_matter("TEST-ONLY other matter").matter_id
    doc = durable.runtime.store.add_document(
        other, "source.txt", b"Synthetic isolated source."
    )
    version = durable.runtime.repo.active_document_version(other, doc.document_id)
    assert version is not None
    response = client.post(
        f"/matters/{other}/investigations",
        json={
            "request_id": "stable-start",
            "approved_versions": [
                {"document_id": doc.document_id, "version_id": version.version_id},
            ],
        },
    )
    assert response.status_code == 201
    assert response.json()["thread_id"] != first["thread_id"]
    assert response.json()["matter_id"] == other


@pytest.mark.parametrize("change", ["version", "bytes"])
def test_retry_still_requires_current_source_integrity(
    client: TestClient, durable: Harness, change: str
) -> None:
    body = payload(durable)
    assert client.post(base(durable), json=body).status_code == 201
    source = durable.runtime.approved[0]
    if change == "version":
        version = durable.runtime.repo.active_document_version(
            durable.runtime.matter, source.document_id
        )
        assert version is not None
        durable.runtime.repo.create_document_version(
            version.model_copy(update={"version_id": "replacement"})
        )
    else:
        path = (
            durable.runtime.store.private_root
            / durable.runtime.matter
            / source.document_id
            / "extracted.txt"
        )
        path.write_text("TEST-ONLY tampered source")
    response = client.post(base(durable), json=body)
    assert response.status_code == 409
    assert response.json() == {"detail": "INVESTIGATION_STATE_CONFLICT"}
    assert len(durable.runtime.repo.list_threads(durable.runtime.matter)) == 1


@pytest.mark.parametrize("key", ["", "contains space", "a" * 257])
def test_invalid_request_identity_is_422(
    client: TestClient, durable: Harness, key: str
) -> None:
    response = client.post(base(durable), json=payload(durable, key))
    assert response.status_code == 422
    assert response.json() == {"detail": "INVESTIGATION_INVALID_REQUEST"}
    assert durable.runtime.repo.list_threads(durable.runtime.matter) == ()

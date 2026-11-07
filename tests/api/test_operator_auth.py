"""Offline pilot authentication, ownership and future thread-boundary contracts."""

from pathlib import Path
from typing import Annotated

import pytest
from fastapi import Depends, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from lextrace.api.app import create_app
from lextrace.api.auth import OperatorPrincipal, require_operator
from lextrace.config import AppSettings
from lextrace.matter.contracts import MatterError
from lextrace.matter.store import MatterStore

SECRET = "test-only-credential-00000000000000000000000000"
HEADERS = {"Authorization": f"Bearer {SECRET}"}


def settings(tmp_path: Path, **changes: object) -> AppSettings:
    values: dict[str, object] = {
        "operator_id": "operator-a",
        "operator_api_secret": SECRET,
        "matter_db": tmp_path / "matters.sqlite3",
        "private_matter_root": tmp_path / "private",
    }
    values.update(changes)
    return AppSettings.model_validate(values)


@pytest.mark.parametrize(
    "token", [None, "", "Basic wrong", "Bearer wrong", "Bearer x x"]
)
def test_invalid_credentials(tmp_path: Path, token: str | None) -> None:
    with TestClient(create_app(settings=settings(tmp_path))) as client:
        headers = {"Authorization": token} if token is not None else {}
        response = client.get("/matters", headers=headers)
        assert response.status_code == 401
        assert response.json() == {"detail": "OPERATOR_AUTH_REQUIRED"}
        assert response.headers["www-authenticate"] == "Bearer"
        assert not (tmp_path / "matters.sqlite3").exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"operator_id": None},
        {"operator_api_secret": None},
        {"operator_api_secret": "short"},
        {"operator_id": "bad identity"},
    ],
)
def test_fail_closed_unconfigured(tmp_path: Path, changes: dict[str, object]) -> None:
    with TestClient(create_app(settings=settings(tmp_path, **changes))) as client:
        assert client.get("/matters", headers=HEADERS).json() == {
            "detail": "OPERATOR_AUTH_UNAVAILABLE"
        }
        assert client.get("/matters", headers=HEADERS).status_code == 503
        assert client.get("/health").status_code == 200
        assert client.get("/openapi.json").status_code == 200


def test_every_route_protected_before_validation(tmp_path: Path) -> None:
    app = create_app(settings=settings(tmp_path))
    with TestClient(app) as client:
        for route in app.routes:
            if not isinstance(route, APIRoute) or route.path == "/health":
                continue
            path = route.path
            for name in route.param_convertors:
                path = path.replace("{" + name + "}", "f" * 32)
            for method in route.methods or set():
                response = client.request(method, path)
                assert response.status_code == 401, (method, path, response.text)
        # Closed default also covers later routers.
        assert client.post("/future-private-route").status_code == 401
        # A valid credential for a different configured identity cannot use any
        # route in a workspace already bound to another operator.
        config = settings(tmp_path)
        store = MatterStore(config.matter_db, config.private_matter_root)
        store.bind_operator("different-owner")
        store.close()
        for route in app.routes:
            if not isinstance(route, APIRoute) or route.path == "/health":
                continue
            path = route.path
            for name in route.param_convertors:
                path = path.replace("{" + name + "}", "f" * 32)
            for method in route.methods or set():
                response = client.request(method, path, headers=HEADERS)
                assert response.status_code == 403, (method, path, response.text)


def test_principal_and_payload_spoofing(tmp_path: Path) -> None:
    app = create_app(settings=settings(tmp_path))

    @app.post("/principal-test")
    def principal_test(
        request: Request,
        principal: Annotated[OperatorPrincipal, Depends(require_operator)],
    ) -> dict[str, str]:
        assert request.state.operator == principal
        return {"reviewer_id": principal.operator_id}

    with TestClient(app, headers=HEADERS) as client:
        assert client.post(
            "/principal-test", json={"reviewer_id": "attacker"}
        ).json() == {"reviewer_id": "operator-a"}
        assert (
            client.post(
                "/matters", json={"name": "Matter", "reviewer_id": "attacker"}
            ).status_code
            == 422
        )
        assert client.post("/matters", json={"name": "Matter"}).status_code == 201
        assert (
            client.get(
                "/matters",
                headers=[
                    ("Authorization", f"Bearer {SECRET}"),
                    ("Authorization", f"Bearer {SECRET}"),
                ],
            ).status_code
            == 401
        )


def test_restart_rotation_and_ownership_mismatch(tmp_path: Path) -> None:
    with TestClient(create_app(settings=settings(tmp_path)), headers=HEADERS) as client:
        matter = client.post("/matters", json={"name": "Private"}).json()
    rotated = "another-test-credential-00000000000000000000000000"
    with TestClient(
        create_app(settings=settings(tmp_path, operator_api_secret=rotated)),
        headers={"Authorization": f"Bearer {rotated}"},
    ) as client:
        assert client.get(f"/matters/{matter['matter_id']}").status_code == 200
    with TestClient(
        create_app(
            settings=settings(
                tmp_path, operator_id="operator-b", adopt_legacy_matters=True
            )
        ),
        headers=HEADERS,
    ) as client:
        for method, path in [
            ("GET", "/matters"),
            ("GET", f"/matters/{matter['matter_id']}"),
            ("POST", "/search"),
            ("GET", "/runs"),
        ]:
            response = client.request(method, path)
            assert response.status_code == 403
            assert response.json() == {"detail": "OPERATOR_ACCESS_DENIED"}


def test_legacy_adoption_explicit(tmp_path: Path) -> None:
    config = settings(tmp_path)
    store = MatterStore(config.matter_db, config.private_matter_root)
    matter = store.create_matter("Legacy")
    store.close()
    with TestClient(create_app(settings=config), headers=HEADERS) as client:
        assert client.get("/matters").status_code == 403
    with TestClient(
        create_app(settings=settings(tmp_path, adopt_legacy_matters=True)),
        headers=HEADERS,
    ) as client:
        assert client.get("/matters").json()[0]["matter_id"] == matter.matter_id
    with TestClient(create_app(settings=config), headers=HEADERS) as client:
        assert client.get("/matters").status_code == 200


def test_trusted_thread_association(tmp_path: Path) -> None:
    config = settings(tmp_path)
    store = MatterStore(config.matter_db, config.private_matter_root)
    a, b = store.create_matter("A"), store.create_matter("B")
    from lextrace.investigations.repository import InvestigationRepository

    repo = InvestigationRepository(config.matter_db, source_store=store)
    # Synthetic durable registry entry, with no model or checkpoint involved.
    with repo._db:
        repo._db.execute(
            "INSERT INTO investigation_runs(thread_id,matter_id,run_id) VALUES (?,?,?)",
            ("thread", a.matter_id, "run"),
        )
    store.require_investigation_matter(a.matter_id, "thread")
    with pytest.raises(MatterError, match="not found"):
        store.require_investigation_matter(b.matter_id, "thread")
    with pytest.raises(MatterError, match="not found"):
        store.require_investigation_matter(a.matter_id, "guessed")
    repo.close()
    store.close()


def test_safe_failure_and_secret_repr(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = settings(tmp_path, matter_db=tmp_path)  # Cannot open directory as SQLite.
    with TestClient(create_app(settings=config)) as client:
        response = client.get("/matters", headers=HEADERS)
        assert response.status_code == 503
        assert response.json() == {"detail": "OPERATOR_STORAGE_UNAVAILABLE"}
    assert SECRET not in repr(config)
    assert SECRET not in config.model_dump_json()
    assert SECRET not in caplog.text
    assert str(tmp_path) not in response.text


def test_preflight(tmp_path: Path) -> None:
    with TestClient(create_app(settings=settings(tmp_path))) as client:
        response = client.options(
            "/matters",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        assert response.status_code == 200
        assert (
            client.get(
                "/matters", headers={"Origin": "http://localhost:3000"}
            ).status_code
            == 401
        )


def test_environment_configuration_and_no_query_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEXTRACE_OPERATOR_ID", "environment-operator")
    monkeypatch.setenv("LEXTRACE_OPERATOR_API_SECRET", SECRET)
    config = AppSettings.from_environment()
    assert config.operator_id == "environment-operator"
    assert config.operator_api_secret is not None
    assert config.operator_api_secret.get_secret_value() == SECRET
    with TestClient(create_app(settings=settings(tmp_path))) as client:
        assert client.get(f"/matters?token={SECRET}").status_code == 401
        assert (
            client.get("/matters", headers={"X-Reviewer-ID": "operator-a"}).status_code
            == 401
        )


def test_concurrent_workspace_binding_and_binding_survives_deletion(
    tmp_path: Path,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    config = settings(tmp_path)
    stores = [
        MatterStore(config.matter_db, config.private_matter_root) for _ in range(2)
    ]

    def bind(index: int) -> bool:
        try:
            stores[index].bind_operator(f"operator-{index}")
            return True
        except MatterError:
            return False

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(bind, range(2)))
        assert sorted(results) == [False, True]
        winner = results.index(True)
        matter = stores[winner].create_matter("Temporary")
        assert stores[winner].delete_matter(matter.matter_id)
        with pytest.raises(MatterError, match="MISMATCH"):
            stores[1 - winner].bind_operator(
                f"operator-{1 - winner}", adopt_legacy=True
            )
    finally:
        for store in stores:
            store.close()

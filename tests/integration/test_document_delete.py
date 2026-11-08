"""Document deletion ownership, cascades and cross-resource failure behavior."""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from lextrace.api.app import create_app
from lextrace.config import AppSettings
from lextrace.matter.contracts import MatterError
from lextrace.matter.store import MatterStore


def test_delete_cascades_and_preserves_neighbors(tmp_path: Path) -> None:
    database = tmp_path / "matter.sqlite3"
    store = MatterStore(database, tmp_path / "private")
    first = store.create_matter("First").matter_id
    second = store.create_matter("Second").matter_id
    target = store.add_document(first, "a.txt", b"Target text.")
    neighbors = [
        store.add_document(first, "b.txt", b"Other text."),
        store.add_document(second, "a.txt", b"Target text."),
    ]
    assert not store.delete_document(second, target.document_id)
    assert not store.delete_document("0" * 32, target.document_id)
    assert not store.delete_document(first, "0" * 32)
    assert store.sections(first, target.document_id)
    assert store.delete_document(first, target.document_id)
    assert store.get_document(first, target.document_id) is None
    assert not (store.private_root / first / target.document_id).exists()
    with sqlite3.connect(database) as db:
        for table in ("document_sections", "investigation_document_versions"):
            assert db.execute(
                f"SELECT count(*) FROM {table} WHERE document_id=?",
                (target.document_id,),
            ).fetchone() == (0,)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    for document in neighbors:
        assert store.get_document(document.matter_id, document.document_id) == document
        assert store.document_text(document.matter_id, document.document_id)
        assert store.sections(document.matter_id, document.document_id)
    store.close()


@pytest.mark.parametrize("status", ["queued", "running", "completed", "failed"])
def test_jobs(tmp_path: Path, status: str) -> None:
    store = MatterStore(tmp_path / "db", tmp_path / "private")
    matter = store.create_matter("Jobs").matter_id
    doc = store.add_document(matter, "a.txt", b"Text.").document_id
    job = store.create_job(matter, doc)
    store.set_job(job, status)
    if status in {"queued", "running"}:
        with pytest.raises(MatterError, match="finish"):
            store.delete_document(matter, doc)
        assert store.document_text(matter, doc) == "Text."
    else:
        assert store.delete_document(matter, doc)
        assert store.job(matter, job) is None
        with pytest.raises(MatterError, match="not found"):
            store.create_job(matter, doc)
    store.close()


@pytest.mark.parametrize(
    "table", ["matter_issues", "matter_facts", "matter_claims", "document_citations"]
)
def test_reference_blockers(tmp_path: Path, table: str) -> None:
    database = tmp_path / "db"
    store = MatterStore(database, tmp_path / "private")
    matter = store.create_matter("References").matter_id
    doc = store.add_document(matter, "a.txt", b"Text.").document_id
    with sqlite3.connect(database) as db:
        db.execute(f"INSERT INTO {table} VALUES (?,?,?,?)", ("ref", matter, doc, "{}"))
    with pytest.raises(MatterError, match="references"):
        store.delete_document(matter, doc)
    assert store.document_text(matter, doc) == "Text."
    store.close()


def test_database_failure_preserves_files(tmp_path: Path) -> None:
    database = tmp_path / "db"
    store = MatterStore(database, tmp_path / "private")
    matter = store.create_matter("Failure").matter_id
    doc = store.add_document(matter, "a.txt", b"Text.").document_id
    with sqlite3.connect(database) as db:
        db.execute(
            "CREATE TRIGGER prevent_delete BEFORE DELETE ON matter_documents "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    with pytest.raises(MatterError, match="metadata deletion failed"):
        store.delete_document(matter, doc)
    assert store.document_text(matter, doc) == "Text."
    assert store.sections(matter, doc)
    store.close()


def test_api_and_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = AppSettings(
        matter_db=tmp_path / "db",
        private_matter_root=tmp_path / "private",
        operator_id="deletion-test-operator",
        operator_api_secret=SecretStr(
            "test-only-credential-00000000000000000000000000"
        ),
    )
    with TestClient(
        create_app(settings=settings),
        headers={
            "Authorization": "Bearer test-only-credential-00000000000000000000000000",
        },
    ) as client:
        matter = client.post("/matters", json={"name": "API"}).json()["matter_id"]
        other = client.post("/matters", json={"name": "Other"}).json()["matter_id"]

        def upload(text: bytes) -> str:
            return str(
                client.post(
                    f"/matters/{matter}/documents",
                    files={"file": ("a.txt", text, "text/plain")},
                ).json()["document_id"]
            )

        doc = upload(b"Text.")
        url = f"/matters/{matter}/documents/{doc}"
        assert client.delete(f"/matters/{other}/documents/{doc}").status_code == 404
        assert client.delete(url).status_code == 204
        assert client.delete(url).status_code == 404
        assert client.get(url).status_code == 404
        doc = upload(b"Private text.")
        url = f"/matters/{matter}/documents/{doc}"
        directory = tmp_path / "private" / matter / doc

        def fail(self: MatterStore, matter_id: str, document_id: str) -> None:
            (self.private_root / matter_id / document_id / "extracted.txt").unlink()
            raise PermissionError("sensitive private path")

        monkeypatch.setattr(MatterStore, "_remove_private_directory", fail)
        response = client.delete(url)
        assert response.status_code == 400
        assert "manual cleanup" in response.json()["detail"]
        assert "sensitive" not in response.text
        assert client.get(url).status_code == 404
        assert (directory / "source.txt").exists()
        monkeypatch.undo()
        assert client.delete(url).status_code == 204
        assert not directory.exists()
        assert client.delete(url).status_code == 404

"""Durable private cleanup across crashes, failures and scoped retries."""

import sqlite3
from pathlib import Path

import pytest

from lextrace.matter.contracts import MatterError
from lextrace.matter.documents import ExtractedDocument, extract_document
from lextrace.matter.store import MatterStore


@pytest.mark.parametrize("whole_matter", [False, True])
def test_crash_restart_and_bounded_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, whole_matter: bool
) -> None:
    database, root = tmp_path / "db", tmp_path / "private"
    store = MatterStore(database, root)
    matter = store.create_matter("Delete").matter_id
    neighbor = store.create_matter("Keep").matter_id
    document = store.add_document(matter, "a.txt", b"Delete me")
    other = store.add_document(neighbor, "a.txt", b"Keep me")
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO monitoring_runs_v1 VALUES ('legacy', 'untouched')")
        # Legacy unrelated payloads remain opaque, including research records.
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute(
            "INSERT INTO research_runs_v2 VALUES ('run', ?, 'legacy', 'untouched')",
            (neighbor,),
        )

    def crash(self: MatterStore, matter_id: str, document_id: str) -> None:
        raise SystemExit("simulated crash after commit")

    with monkeypatch.context() as patch:
        patch.setattr(MatterStore, "_remove_private_directory", crash)
        with pytest.raises(SystemExit):
            if whole_matter:
                store.delete_matter(matter)
            else:
                store.delete_document(matter, document.document_id)
    store.close()
    store = MatterStore(database, root)
    assert store.get_document(matter, document.document_id) is None
    assert (root / matter / document.document_id).exists()
    assert not store.delete_document(neighbor, document.document_id)

    def fail(self: MatterStore, matter_id: str, document_id: str) -> None:
        raise PermissionError("private secret")

    with monkeypatch.context() as patch:
        patch.setattr(MatterStore, "_remove_private_directory", fail)
        assert store.recover_pending_cleanup(limit=1) == (0, 1)
        with pytest.raises(MatterError, match="cleanup failed"):
            if whole_matter:
                store.delete_matter(matter)
            else:
                store.delete_document(matter, document.document_id)
    assert store.recover_pending_cleanup(limit=1) == (1, 0)
    assert store.recover_pending_cleanup() == (0, 0)
    assert not (root / matter / document.document_id).exists()
    assert store.document_text(neighbor, other.document_id) == "Keep me"
    assert store.sections(neighbor, other.document_id)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT * FROM monitoring_runs_v1").fetchall() == [
            ("legacy", "untouched")
        ]
        assert db.execute("SELECT payload FROM research_runs_v2").fetchall() == [
            ("untouched",)
        ]
        assert db.execute(
            "SELECT count(*) FROM investigation_document_versions WHERE matter_id=?",
            (matter,),
        ).fetchone() == (0,)
    if whole_matter:
        with pytest.raises(MatterError, match="not found"):
            store.add_document(matter, "late.txt", b"Late")
    with pytest.raises(MatterError, match="not found"):
        store.create_job(matter, document.document_id)
    store.close()


@pytest.mark.parametrize("whole_matter", [False, True])
def test_symlink_ancestor_never_removes_neighbor(
    tmp_path: Path, whole_matter: bool
) -> None:
    store = MatterStore(tmp_path / "db", tmp_path / "private")
    matter = store.create_matter("Delete").matter_id
    other = store.create_matter("Keep").matter_id
    doc = store.add_document(matter, "a.txt", b"Delete")
    neighbor = store.add_document(other, "b.txt", b"Keep")
    directory = store.private_root / matter
    moved = tmp_path / "displaced"
    directory.rename(moved)
    directory.symlink_to(store.private_root / other, target_is_directory=True)
    with pytest.raises(MatterError, match="cleanup failed"):
        if whole_matter:
            store.delete_matter(matter)
        else:
            store.delete_document(matter, doc.document_id)
    assert store.document_text(other, neighbor.document_id) == "Keep"
    directory.unlink()
    moved.rename(directory)
    assert store.recover_pending_cleanup() == (1, 0)
    assert store.document_text(other, neighbor.document_id) == "Keep"
    store.close()


@pytest.mark.parametrize("status", ["queued", "running"])
def test_matter_active_job_and_sql_rollback(tmp_path: Path, status: str) -> None:
    database = tmp_path / "db"
    store = MatterStore(database, tmp_path / "private")
    matter = store.create_matter("Active").matter_id
    document = store.add_document(matter, "a.txt", b"Text")
    job = store.create_job(matter, document.document_id)
    store.set_job(job, status)
    with pytest.raises(MatterError, match="finish"):
        store.delete_matter(matter)
    assert store.recover_pending_cleanup() == (0, 0)
    store.set_job(job, "completed")
    with sqlite3.connect(database) as db:
        db.execute(
            "CREATE TRIGGER prevent_delete BEFORE DELETE ON matters "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    with pytest.raises(MatterError, match="metadata deletion failed"):
        store.delete_matter(matter)
    assert store.document_text(matter, document.document_id) == "Text"
    assert store.recover_pending_cleanup() == (0, 0)
    store.close()


def test_missing_directory_and_bounded_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MatterStore(tmp_path / "db", tmp_path / "private")
    matters = [store.create_matter(str(i)).matter_id for i in range(2)]

    def crash(self: MatterStore, matter_id: str, document_id: str) -> None:
        raise SystemExit

    with monkeypatch.context() as patch:
        patch.setattr(MatterStore, "_remove_private_directory", crash)
        for matter in matters:
            with pytest.raises(SystemExit):
                store.delete_matter(matter)
    # No directory ever existed: covers crash after removal but before ledger ack.
    assert store.recover_pending_cleanup(limit=1) == (1, 0)
    assert store.recover_pending_cleanup(limit=1) == (1, 0)
    assert store.recover_pending_cleanup(limit=1) == (0, 0)
    with pytest.raises(ValueError):
        store.recover_pending_cleanup(limit=0)
    store.close()


def test_upload_rechecks_owner_after_concurrent_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lextrace.matter import store as module

    database, root = tmp_path / "db", tmp_path / "private"
    store = MatterStore(database, root)
    other_connection = MatterStore(database, root)
    matter = store.create_matter("Race").matter_id

    def delete_during_extraction(filename: str, content: bytes) -> ExtractedDocument:
        result = extract_document(filename, content)
        assert other_connection.delete_matter(matter)
        return result

    monkeypatch.setattr(module, "extract_document", delete_during_extraction)
    with pytest.raises(MatterError):
        store.add_document(matter, "late.txt", b"Late write")
    assert store.get_matter(matter) is None
    assert store.list_documents(matter) == []
    assert not (root / matter).exists()
    store.close()
    other_connection.close()


def test_child_symlink_is_unlinked_without_following(tmp_path: Path) -> None:
    store = MatterStore(tmp_path / "db", tmp_path / "private")
    matter = store.create_matter("Delete").matter_id
    document = store.add_document(matter, "a.txt", b"Text")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("Keep")
    (store.private_root / matter / document.document_id / "link").symlink_to(
        outside, target_is_directory=True
    )
    assert store.delete_document(matter, document.document_id)
    assert (outside / "keep").read_text() == "Keep"
    store.close()


def test_document_root_symlink_cannot_delete_other_document(tmp_path: Path) -> None:
    store = MatterStore(tmp_path / "db", tmp_path / "private")
    matter = store.create_matter("Documents").matter_id
    target = store.add_document(matter, "a.txt", b"Delete")
    neighbor = store.add_document(matter, "b.txt", b"Keep")
    directory = store.private_root / matter / target.document_id
    displaced = tmp_path / "displaced"
    directory.rename(displaced)
    directory.symlink_to(
        directory.parent / neighbor.document_id, target_is_directory=True
    )
    with pytest.raises(MatterError, match="cleanup failed"):
        store.delete_document(matter, target.document_id)
    assert store.document_text(matter, neighbor.document_id) == "Keep"
    directory.unlink()
    displaced.rename(directory)
    assert store.delete_document(matter, target.document_id)
    assert store.document_text(matter, neighbor.document_id) == "Keep"
    store.close()

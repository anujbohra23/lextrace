"""Immutable investigation manifests in the existing local matter database."""

import sqlite3
import threading
from pathlib import Path

from lextrace.investigations.contracts import DocumentVersion
from lextrace.investigations.migrations import migrate
from lextrace.matter.contracts import MatterError


class InvestigationRepository:
    """Owns a separate connection; initialize the database with MatterStore first.

    Close this repository independently of MatterStore. No source text or private
    artifacts are written here. Lists use deterministic opaque version-ID order.
    """

    def __init__(self, database: Path) -> None:
        self._lock = threading.RLock()
        self._db = sqlite3.connect(
            f"{database.resolve().as_uri()}?mode=rw", uri=True, check_same_thread=False
        )
        try:
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA journal_mode=WAL")
            migrate(self._db)
        except Exception:
            self._db.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def create_document_version(self, version: DocumentVersion) -> DocumentVersion:
        """Insert once, reuse an equal manifest, or reject a conflicting identity."""
        version = DocumentVersion.model_validate(version.model_dump(), strict=True)
        identity = (version.matter_id, version.document_id, version.version_id)
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                owner = self._db.execute(
                    "SELECT 1 FROM matter_documents d "
                    "JOIN matters m ON m.id=d.matter_id "
                    "WHERE d.matter_id=? AND d.id=?",
                    identity[:2],
                ).fetchone()
                if owner is None:
                    raise MatterError("Document was not found.")
                row = self._db.execute(
                    "SELECT payload FROM investigation_document_versions "
                    "WHERE matter_id=? AND document_id=? AND version_id=?",
                    identity,
                ).fetchone()
                if row is not None:
                    stored = DocumentVersion.model_validate_json(row[0])
                    if stored != version:
                        raise MatterError("Document version identity conflicts.")
                    result = stored
                else:
                    self._db.execute(
                        "INSERT INTO investigation_document_versions "
                        "(matter_id,document_id,version_id,payload) VALUES (?,?,?,?)",
                        (*identity, version.model_dump_json()),
                    )
                    result = version
                self._db.commit()
                return result
            except Exception:
                self._db.rollback()
                raise

    def get_document_version(
        self, matter_id: str, document_id: str, version_id: str
    ) -> DocumentVersion | None:
        with self._lock:
            row = self._db.execute(
                "SELECT v.payload FROM investigation_document_versions v "
                "JOIN matter_documents d ON d.id=v.document_id "
                "AND d.matter_id=v.matter_id JOIN matters m ON m.id=d.matter_id "
                "WHERE v.matter_id=? AND v.document_id=? AND v.version_id=?",
                (matter_id, document_id, version_id),
            ).fetchone()
        return DocumentVersion.model_validate_json(row[0]) if row else None

    def list_document_versions(
        self, matter_id: str, document_id: str
    ) -> list[DocumentVersion]:
        with self._lock:
            rows = self._db.execute(
                "SELECT v.payload FROM investigation_document_versions v "
                "JOIN matter_documents d ON d.id=v.document_id "
                "AND d.matter_id=v.matter_id JOIN matters m ON m.id=d.matter_id "
                "WHERE v.matter_id=? AND v.document_id=? ORDER BY v.version_id",
                (matter_id, document_id),
            ).fetchall()
        return [DocumentVersion.model_validate_json(row[0]) for row in rows]

"""Numbered, additive investigation migrations for an initialized matter DB."""

import sqlite3


def migrate(connection: sqlite3.Connection) -> None:
    """Apply schema changes atomically; caller owns an idle connection.

    BEGIN IMMEDIATE serializes initialization across repository instances. Avoid
    executescript here because it can implicitly commit a pending transaction.
    """
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS investigation_schema_versions "
            "(version INTEGER PRIMARY KEY)"
        )
        applied = connection.execute(
            "SELECT 1 FROM investigation_schema_versions WHERE version=1"
        ).fetchone()
        if applied is None:
            connection.execute(
                "CREATE UNIQUE INDEX investigation_document_ownership "
                "ON matter_documents(matter_id,id)"
            )
            connection.execute(
                """CREATE TABLE investigation_document_versions (
                    matter_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (matter_id,document_id,version_id),
                    FOREIGN KEY (matter_id) REFERENCES matters(id) ON DELETE CASCADE,
                    FOREIGN KEY (matter_id,document_id)
                        REFERENCES matter_documents(matter_id,id) ON DELETE CASCADE
                )"""
            )
            connection.execute(
                "INSERT INTO investigation_schema_versions(version) VALUES (?)", (1,)
            )
        if (
            connection.execute(
                "SELECT 1 FROM investigation_schema_versions WHERE version=2"
            ).fetchone()
            is None
        ):
            # Preserve legacy section IDs and payloads while scoping storage keys.
            connection.execute(
                "CREATE TABLE document_sections_scoped ("
                "id TEXT NOT NULL, document_id TEXT NOT NULL, payload TEXT NOT NULL, "
                "PRIMARY KEY(document_id,id), FOREIGN KEY(document_id) "
                "REFERENCES matter_documents(id) ON DELETE CASCADE)"
            )
            connection.execute(
                "INSERT INTO document_sections_scoped SELECT * FROM document_sections"
            )
            connection.execute("DROP TABLE document_sections")
            connection.execute(
                "ALTER TABLE document_sections_scoped RENAME TO document_sections"
            )
            connection.execute(
                "INSERT INTO investigation_schema_versions(version) VALUES (2)"
            )
        if (
            connection.execute(
                "SELECT 1 FROM investigation_schema_versions WHERE version=3"
            ).fetchone()
            is None
        ):
            # Audit payloads intentionally outlive document deletion. Current reads
            # must resolve live ownership and active source versions separately.
            for statement in (
                "CREATE TABLE investigation_review_revisions ("
                "matter_id TEXT NOT NULL, target TEXT NOT NULL, revision INTEGER "
                "NOT NULL, payload TEXT NOT NULL, "
                "PRIMARY KEY(matter_id,target,revision))",
                "CREATE TABLE investigation_review_decisions ("
                "matter_id TEXT NOT NULL, decision_id TEXT NOT NULL, "
                "target TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT "
                "NOT NULL, "
                "PRIMARY KEY(matter_id,decision_id))",
                "CREATE TABLE investigation_active_sources ("
                "matter_id TEXT NOT NULL, document_id TEXT NOT NULL, "
                "version_id TEXT NOT NULL, PRIMARY KEY(matter_id,document_id))",
                "CREATE TABLE investigation_retired_sources ("
                "matter_id TEXT NOT NULL, document_id TEXT NOT NULL, "
                "version_id TEXT NOT NULL, PRIMARY "
                "KEY(matter_id,document_id,version_id))",
                "CREATE TRIGGER investigation_source_deleted BEFORE DELETE "
                "ON matter_documents BEGIN "
                "INSERT OR IGNORE INTO investigation_retired_sources "
                "SELECT matter_id,document_id,version_id FROM "
                "investigation_document_versions WHERE matter_id=OLD.matter_id "
                "AND document_id=OLD.id; "
                "DELETE FROM investigation_active_sources WHERE "
                "matter_id=OLD.matter_id "
                "AND document_id=OLD.id; END",
                "CREATE TABLE investigation_projection_dependencies ("
                "matter_id TEXT NOT NULL, projection_id TEXT NOT NULL, "
                "payload TEXT NOT NULL, PRIMARY KEY(matter_id,projection_id))",
            ):
                connection.execute(statement)
            # Only a sole historical version can be identified without guessing
            # chronological meaning from opaque IDs. Ambiguous sources fail closed.
            connection.execute(
                "INSERT INTO investigation_active_sources "
                "SELECT matter_id,document_id,version_id FROM "
                "investigation_document_versions GROUP BY matter_id,document_id "
                "HAVING COUNT(*)=1"
            )
            connection.execute(
                "INSERT INTO investigation_schema_versions(version) VALUES (3)"
            )
        if (
            connection.execute(
                "SELECT 1 FROM investigation_schema_versions WHERE version=4"
            ).fetchone()
            is None
        ):
            for statement in (
                "CREATE TABLE investigation_runs (thread_id TEXT PRIMARY KEY, "
                "matter_id TEXT NOT NULL REFERENCES matters(id) ON DELETE CASCADE, "
                "run_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'LIVE')",
                "CREATE TABLE investigation_run_sources (thread_id TEXT NOT NULL "
                "REFERENCES investigation_runs(thread_id) ON DELETE CASCADE, "
                "document_id TEXT NOT NULL, PRIMARY KEY(thread_id,document_id))",
                "CREATE TABLE investigation_artifacts (thread_id TEXT NOT NULL "
                "REFERENCES investigation_runs(thread_id) ON DELETE CASCADE, "
                "node TEXT NOT NULL, digest TEXT NOT NULL, payload BLOB NOT NULL, "
                "result TEXT, PRIMARY KEY(thread_id,node))",
                "CREATE TABLE investigation_intents (thread_id TEXT NOT NULL "
                "REFERENCES investigation_runs(thread_id) ON DELETE CASCADE, "
                "gate TEXT NOT NULL, token TEXT NOT NULL UNIQUE, payload TEXT "
                "NOT NULL, "
                "status TEXT NOT NULL, outcome TEXT, PRIMARY KEY(thread_id,gate))",
                "CREATE TABLE investigation_snapshots (thread_id TEXT PRIMARY KEY "
                "REFERENCES investigation_runs(thread_id) ON DELETE CASCADE, "
                "snapshot_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL)",
                "CREATE TABLE investigation_checkpoint_cleanup (thread_id TEXT "
                "PRIMARY KEY)",
                "CREATE TRIGGER investigation_run_deleted BEFORE DELETE ON "
                "investigation_runs BEGIN INSERT OR IGNORE INTO "
                "investigation_checkpoint_cleanup VALUES (OLD.thread_id); END",
                "CREATE TRIGGER investigation_run_source_deleted BEFORE DELETE ON "
                "matter_documents BEGIN DELETE FROM investigation_runs WHERE "
                "matter_id=OLD.matter_id AND thread_id IN (SELECT thread_id FROM "
                "investigation_run_sources WHERE document_id=OLD.id); END",
            ):
                connection.execute(statement)
            connection.execute(
                "INSERT INTO investigation_schema_versions(version) VALUES (4)"
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise

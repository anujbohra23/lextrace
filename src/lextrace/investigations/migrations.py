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
        connection.commit()
    except Exception:
        connection.rollback()
        raise

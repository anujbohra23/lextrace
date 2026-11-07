"""Focused immutable manifest storage and legacy migration regressions."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lextrace.investigations.contracts import DocumentVersion
from lextrace.investigations.repository import InvestigationRepository
from lextrace.matter.contracts import MatterDocument, MatterError
from lextrace.matter.store import MatterStore


@pytest.fixture
def legacy(tmp_path: Path) -> tuple[Path, list[MatterDocument]]:
    database = tmp_path / "matter.sqlite3"
    store = MatterStore(database, tmp_path / "private")
    documents = []
    # Seed metadata without upload integration: legacy section IDs can collide
    # for identical content across matters, which is outside this storage slice.
    for number in range(2):
        matter = store.create_matter(f"Matter {number}")
        document = MatterDocument(
            document_id=str(number) * 32,
            matter_id=matter.matter_id,
            filename="source.pdf",
            document_type="pdf",
            content_hash="a" * 64,
            ingestion_status="READY",
            page_count=3,
            created_at=datetime.now(UTC),
            text_length=12,
        )
        documents.append(document)
        with sqlite3.connect(database) as connection:
            connection.execute(
                "INSERT INTO matter_documents VALUES (?,?,?)",
                (document.document_id, document.matter_id, document.model_dump_json()),
            )
    store.close()
    return database, documents


def manifest(document: MatterDocument, version_id: str = "v1") -> DocumentVersion:
    return DocumentVersion(
        matter_id=document.matter_id,
        document_id=document.document_id,
        version_id=version_id,
        original_content_hash=document.content_hash,
        extracted_text_hash="b" * 64,
        parser="synthetic parser",
        parser_version="1.2",
        extraction_settings_hash="c" * 64,
        document_type="pdf",
        text_length=12,
        page_starts=(0, 0, 8),
        extraction_warnings=("Empty page", "Unicode 😀 preserved"),
        coverage="PARTIAL",
    )


def test_round_trip_history_and_reopen(
    legacy: tuple[Path, list[MatterDocument]],
) -> None:
    database, documents = legacy
    first = manifest(documents[0])
    second = manifest(documents[0], "v2").model_copy(update={"parser_version": "2"})
    repository = InvestigationRepository(database)
    assert repository.create_document_version(first) == first
    assert repository.create_document_version(second) == second
    assert (
        repository.get_document_version(
            first.matter_id, first.document_id, first.version_id
        )
        == first
    )
    assert repository.list_document_versions(first.matter_id, first.document_id) == [
        first,
        second,
    ]
    repository.close()
    repository = InvestigationRepository(database)
    restored = repository.get_document_version(
        first.matter_id, first.document_id, first.version_id
    )
    assert restored is not None
    assert restored.model_dump() == first.model_dump()
    assert repository.list_document_versions(first.matter_id, first.document_id) == [
        first,
        second,
    ]
    repository.close()


def test_ownership_and_identical_content(
    legacy: tuple[Path, list[MatterDocument]],
) -> None:
    database, documents = legacy
    repository = InvestigationRepository(database)
    first, second = [manifest(document) for document in documents]
    for version in (first, second):
        repository.create_document_version(version)
        assert (
            repository.get_document_version(
                version.matter_id, version.document_id, version.version_id
            )
            == version
        )
    for matter_id, document_id in (
        (second.matter_id, first.document_id),
        (first.matter_id, second.document_id),
        ("missing", first.document_id),
        (first.matter_id, "missing"),
        ("' OR 1=1 --", first.document_id),
    ):
        assert repository.get_document_version(matter_id, document_id, "v1") is None
        assert repository.list_document_versions(matter_id, document_id) == []
        with pytest.raises(MatterError, match="not found"):
            repository.create_document_version(
                first.model_copy(
                    update={"matter_id": matter_id, "document_id": document_id}
                )
            )
    repository.close()


def test_duplicate_and_conflict(legacy: tuple[Path, list[MatterDocument]]) -> None:
    database, documents = legacy
    repository = InvestigationRepository(database)
    version = manifest(documents[0])
    assert repository.create_document_version(version) == version
    assert repository.create_document_version(version) == version
    with pytest.raises(MatterError, match="conflicts"):
        repository.create_document_version(
            version.model_copy(update={"extraction_warnings": ("changed",)})
        )
    assert repository.list_document_versions(
        version.matter_id, version.document_id
    ) == [version]
    # A rejected write rolls back and leaves the connection usable.
    repository.create_document_version(manifest(documents[0], "v2"))
    repository.close()


def test_legacy_migration_preserves_records(
    legacy: tuple[Path, list[MatterDocument]], tmp_path: Path
) -> None:
    database, documents = legacy
    with sqlite3.connect(database) as connection:
        # Existing JSON payloads remain opaque to the migration.
        connection.execute("PRAGMA foreign_keys=OFF")
        for sql in (
            "INSERT INTO research_runs_v2 VALUES ('run','matter','claim','research')",
            "INSERT INTO document_citations VALUES ('cite','matter','doc','citation')",
            "INSERT INTO monitoring_runs_v1 VALUES ('monitor','monitoring')",
        ):
            connection.execute(sql)
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        before = {
            table: connection.execute(f'SELECT * FROM "{table}"').fetchall()
            for table in tables
        }
    for _ in range(2):
        repository = InvestigationRepository(database)
        repository.create_document_version(manifest(documents[0]))
        repository.close()
        store = MatterStore(database, tmp_path / "private")
        assert (
            store.get_document(documents[0].matter_id, documents[0].document_id)
            == documents[0]
        )
        store.close()
    with sqlite3.connect(database) as connection:
        for table in tables:
            assert (
                connection.execute(f'SELECT * FROM "{table}"').fetchall()
                == before[table]
            )
        assert connection.execute(
            "SELECT version FROM investigation_schema_versions"
        ).fetchall() == [(1,), (2,), (3,)]
        assert connection.execute(
            "SELECT count(*) FROM investigation_document_versions"
        ).fetchone() == (1,)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_concurrent_initialization_and_duplicate_writes(
    legacy: tuple[Path, list[MatterDocument]],
) -> None:
    database, documents = legacy
    version = manifest(documents[0])

    def write() -> DocumentVersion:
        repository = InvestigationRepository(database)
        try:
            return repository.create_document_version(version)
        finally:
            repository.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(lambda _: write(), range(2))) == [version, version]
    repository = InvestigationRepository(database)
    assert repository.list_document_versions(
        version.matter_id, version.document_id
    ) == [version]
    repository.close()


def test_migration_three_preserves_ambiguous_legacy_versions(
    legacy: tuple[Path, list[MatterDocument]],
) -> None:
    from datetime import date

    from lextrace.investigations.review import target_of
    from tests.unit.test_investigation_chronology import event

    database, documents = legacy
    repository = InvestigationRepository(database)
    versions = [manifest(documents[0], identity) for identity in ("version", "aaa")]
    for version in versions:
        repository.create_document_version(version)
    sole = manifest(documents[1], "sole")
    repository.create_document_version(sole)
    repository.close()
    # Reconstruct the populated pre-M4C schema, retaining all manifests and owners.
    with sqlite3.connect(database) as db:
        db.execute("DROP TRIGGER investigation_source_deleted")
        for table in (
            "investigation_review_revisions",
            "investigation_review_decisions",
            "investigation_active_sources",
            "investigation_retired_sources",
            "investigation_projection_dependencies",
        ):
            db.execute(f"DROP TABLE {table}")
        db.execute("DELETE FROM investigation_schema_versions WHERE version=3")
    candidate = event(
        "legacy",
        date(2020, 1, 1),
        matter_id=documents[0].matter_id,
        document_id=documents[0].document_id,
    )
    for _ in range(2):
        repository = InvestigationRepository(database)
        assert repository.list_document_versions(
            candidate.matter_id, candidate.document_id
        ) == sorted(versions, key=lambda version: version.version_id)
        with sqlite3.connect(database) as db:
            assert db.execute(
                "SELECT * FROM investigation_active_sources"
            ).fetchall() == [(sole.matter_id, sole.document_id, sole.version_id)]
        for version in versions:
            ambiguous = candidate.model_copy(
                update={
                    "version_id": version.version_id,
                    "evidence_references": tuple(
                        ref.model_copy(update={"version_id": version.version_id})
                        for ref in candidate.evidence_references
                    ),
                    "temporal": candidate.temporal.model_copy(
                        update={
                            "version_id": version.version_id,
                            "evidence_references": tuple(
                                ref.model_copy(
                                    update={"version_id": version.version_id}
                                )
                                for ref in candidate.temporal.evidence_references
                            ),
                        }
                    ),
                }
            )
            with pytest.raises(MatterError, match="no unambiguous active version"):
                repository.register_review_candidate(ambiguous)
            assert repository.review_history(target_of(ambiguous)) == ()
        repository.close()

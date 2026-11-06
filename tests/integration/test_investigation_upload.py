"""Upload integration preserves source identity and atomic manifest persistence."""

import hashlib
import io
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest
from pypdf import PdfWriter

from lextrace.investigations.contracts import DocumentVersion
from lextrace.investigations.repository import InvestigationRepository
from lextrace.matter.contracts import MatterError
from lextrace.matter.documents import ExtractedDocument
from lextrace.matter.store import MatterStore


def test_upload_identity_isolation_and_reopen(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    private = tmp_path / "private"
    store = MatterStore(database, private)
    source = b"\xef\xbb\xbfFirst\r\n\r\nUnicode \xf0\x9f\x98\x80."
    text = source.decode("utf-8-sig")
    documents = []
    for name in ("First", "Second"):
        matter = store.create_matter(name)
        document = store.add_document(matter.matter_id, "source.txt", source)
        assert store.add_document(matter.matter_id, "copy.txt", source) == document
        assert store.document_text(matter.matter_id, document.document_id) == text
        documents.append(document)
    store.close()
    repository = InvestigationRepository(database)
    versions = []
    for document in documents:
        rows = repository.list_document_versions(
            document.matter_id, document.document_id
        )
        assert len(rows) == 1
        manifest = rows[0]
        versions.append(manifest)
        assert manifest.original_content_hash == hashlib.sha256(source).hexdigest()
        assert manifest.extracted_text_hash == hashlib.sha256(text.encode()).hexdigest()
        assert manifest.text_length == len(text)
        assert manifest.parser == "lextrace.matter.documents/txt"
        assert manifest.parser_version.startswith("1;python=")
        settings = {"algorithm": 1, "kind": "txt", "encoding": "utf-8-sig"}
        assert (
            manifest.extraction_settings_hash
            == hashlib.sha256(
                json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )
        assert manifest.page_starts == ()
        assert manifest.coverage == "COMPLETE"
        assert manifest.extraction_warnings == ()
        directory = private / document.matter_id / document.document_id
        assert (directory / "source.txt").read_bytes() == source
        assert (directory / "extracted.txt").read_bytes() == text.encode()
    assert versions[0].version_id != versions[1].version_id
    assert (
        repository.list_document_versions(
            documents[0].matter_id, documents[1].document_id
        )
        == []
    )
    repository.close()
    store = MatterStore(database, private)
    for document in documents:
        sections = store.sections(document.matter_id, document.document_id)
        assert all(text[s.span.start : s.span.end] == s.text for s in sections)
    store.close()


def test_ocr_and_failed_extraction(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    store = MatterStore(database, tmp_path / "private")
    matter = store.create_matter("Scanned")
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_blank_page(width=200, height=200)
    output = io.BytesIO()
    writer.write(output)
    document = store.add_document(matter.matter_id, "scan.pdf", output.getvalue())
    assert document.ingestion_status == "OCR_REQUIRED"
    repository = InvestigationRepository(database)
    manifest = repository.list_document_versions(
        matter.matter_id, document.document_id
    )[0]
    assert manifest.coverage == "OCR_REQUIRED"
    assert manifest.page_starts == (0, 2)
    assert manifest.text_length == 2
    assert manifest.extraction_warnings
    for filename, content in (("bad.pdf", b"broken"), ("bad.txt", b"\xff")):
        with pytest.raises(MatterError):
            store.add_document(matter.matter_id, filename, content)
    assert store.list_documents(matter.matter_id) == [document]
    repository.close()
    store.close()


def test_registration_failure_rolls_back_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "runtime.sqlite3"
    private = tmp_path / "private"
    store = MatterStore(database, private)
    matter = store.create_matter("Failure")
    original = InvestigationRepository.register_in_transaction

    def fail(
        connection: sqlite3.Connection, manifest: DocumentVersion
    ) -> DocumentVersion:
        original(connection, manifest)
        raise sqlite3.OperationalError("injected after insertion")

    with monkeypatch.context() as patch:
        patch.setattr(InvestigationRepository, "register_in_transaction", fail)
        with pytest.raises(MatterError, match="registration failed"):
            store.add_document(matter.matter_id, "source.txt", b"Private text.")
    assert store.list_documents(matter.matter_id) == []
    assert list((private / matter.matter_id).iterdir()) == []
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM investigation_document_versions"
        ).fetchone() == (0,)
    document = store.add_document(matter.matter_id, "source.txt", b"Private text.")
    assert document.ingestion_status == "READY"
    store.close()


def test_invalid_extracted_unicode_is_not_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MatterStore(tmp_path / "runtime.sqlite3", tmp_path / "private")
    matter = store.create_matter("Invalid")
    monkeypatch.setattr(
        "lextrace.matter.store.extract_document",
        lambda *_: ExtractedDocument("\ud800", (0,), None),
    )
    with pytest.raises(MatterError, match="invalid Unicode"):
        store.add_document(matter.matter_id, "source.txt", b"text")
    assert store.list_documents(matter.matter_id) == []
    store.close()


def test_docx_coverage_and_legacy_duplicate(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    store = MatterStore(database, tmp_path / "private")
    matter = store.create_matter("DOCX")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
            "Exact body text.</w:t></w:r></w:p></w:body></w:document>",
        )
    document = store.add_document(matter.matter_id, "source.docx", output.getvalue())
    repository = InvestigationRepository(database)
    manifest = repository.list_document_versions(
        matter.matter_id, document.document_id
    )[0]
    assert manifest.coverage == "PARTIAL"
    assert manifest.page_starts == ()
    assert manifest.parser == "lextrace.matter.documents/docx"
    assert manifest.text_length == len("Exact body text.")
    assert manifest.extraction_warnings
    # Simulate a pre-integration document, then reuse the ordinary upload path.
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM investigation_document_versions")
    assert (
        store.add_document(matter.matter_id, "copy.docx", output.getvalue()) == document
    )
    assert repository.list_document_versions(
        matter.matter_id, document.document_id
    ) == [manifest]
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM investigation_document_versions")
    saved = (
        tmp_path / "private" / matter.matter_id / document.document_id / "extracted.txt"
    )
    saved.write_bytes(b"changed")
    with pytest.raises(MatterError, match="registration failed"):
        store.add_document(matter.matter_id, "copy.docx", output.getvalue())
    assert (
        repository.list_document_versions(matter.matter_id, document.document_id) == []
    )
    repository.close()
    store.close()


def test_private_file_failure_does_not_register(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "runtime.sqlite3"
    store = MatterStore(database, tmp_path / "private")
    matter = store.create_matter("Storage failure")

    def fail_write(self: Path, data: bytes) -> int:
        raise OSError("injected private path failure")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "write_bytes", fail_write)
        with pytest.raises(MatterError, match="storage or investigation"):
            store.add_document(matter.matter_id, "source.txt", b"Private text.")
    assert store.list_documents(matter.matter_id) == []
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM investigation_document_versions"
        ).fetchone() == (0,)
    store.close()

"""Private document extraction, stable spans, and malformed-input boundaries."""

import io
import zipfile
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from lextrace.matter.contracts import MatterError
from lextrace.matter.documents import extract_document, segment_document
from lextrace.matter.store import MatterStore


def test_text_spans_and_private_deletion(tmp_path: Path) -> None:
    store = MatterStore(tmp_path / "runtime.sqlite3", tmp_path / "private")
    matter = store.create_matter("Smith v. Example", court="S.D.N.Y.")
    text = (
        "INTRODUCTION\n\nA plaintiff must show protected activity.\n\nSecond paragraph."
    )
    document = store.add_document(matter.matter_id, "brief.txt", text.encode())
    duplicate = store.add_document(matter.matter_id, "copy.txt", text.encode())
    assert duplicate.document_id == document.document_id
    sections = store.sections(matter.matter_id, document.document_id)
    assert document.ingestion_status == "READY"
    assert [s.kind for s in sections] == ["heading", "paragraph", "paragraph"]
    assert all(text[s.span.start : s.span.end] == s.text for s in sections)
    assert sections == segment_document(
        document.document_id,
        document.content_hash,
        extract_document("brief.txt", text.encode()),
    )
    root = tmp_path / "private" / matter.matter_id
    assert root.exists()
    assert store.delete_matter(matter.matter_id)
    assert not root.exists()
    store.close()


def test_delete_removes_private_cache_and_rejects_active_job(tmp_path: Path) -> None:
    store = MatterStore(tmp_path / "runtime.sqlite3", tmp_path / "private")
    matter = store.create_matter("Private")
    document = store.add_document(matter.matter_id, "brief.md", b"A legal proposition.")
    cache = tmp_path / "private" / matter.matter_id / "structured-cache.sqlite3"
    cache.write_text("private cached response")
    job_id = store.create_job(matter.matter_id, document.document_id)
    with pytest.raises(MatterError, match="finish"):
        store.delete_matter(matter.matter_id)
    store.set_job(job_id, "failed", "TEST")
    assert store.delete_matter(matter.matter_id)
    assert not cache.exists()
    store.close()


def test_docx_and_scanned_pdf() -> None:
    xml = (
        b'<w:document xmlns:w="http://schemas.openxmlformats.org/'
        b'wordprocessingml/2006/main"><w:body>'
        b"<w:p><w:r><w:t>First</w:t></w:r>"
        b"<w:r><w:t> paragraph</w:t></w:r></w:p>"
        b"<w:p><w:r><w:t>Second.</w:t></w:r></w:p>"
        b"</w:body></w:document>"
    )
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("word/document.xml", xml)
    assert (
        extract_document("motion.docx", stream.getvalue()).text
        == "First paragraph\n\nSecond."
    )
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    pdf = io.BytesIO()
    writer.write(pdf)
    extracted = extract_document("scanned.pdf", pdf.getvalue())
    assert extracted.ocr_required
    assert extracted.page_count == 1


def test_extractable_pdf_has_page_and_exact_section_span() -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/F1"): writer._add_object(font)}
            )
        }
    )
    stream = DecodedStreamObject()
    stream.set_data(
        b"BT /F1 12 Tf 20 200 Td "
        b"(The court held that protected activity matters.) Tj ET"
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    extracted = extract_document("motion.pdf", output.getvalue())
    assert not extracted.ocr_required
    assert extracted.page_count == 1
    sections = segment_document("doc", "hash", extracted)
    assert sections[0].span.page == 1
    assert (
        extracted.text[sections[0].span.start : sections[0].span.end]
        == sections[0].text
    )


@pytest.mark.parametrize("name", ["../secret.txt", "folder\\secret.txt", "bad.exe"])
def test_invalid_filename(name: str) -> None:
    with pytest.raises(MatterError):
        extract_document(name, b"content")


def test_malformed_and_injection_docx() -> None:
    with pytest.raises(MatterError, match="malformed"):
        extract_document("bad.pdf", b"not a pdf")
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("word/document.xml", b"<!DOCTYPE foo><foo/>")
    with pytest.raises(MatterError, match="malformed"):
        extract_document("bad.docx", stream.getvalue())

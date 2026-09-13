"""Bounded private document extraction with exact extracted-text offsets."""

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import PurePath
from xml.etree import ElementTree

from lextrace.matter.contracts import (
    DocumentKind,
    DocumentSection,
    MatterError,
    SourceSpan,
)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ALLOWED_EXTENSIONS: dict[str, DocumentKind] = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".txt": "txt",
    ".md": "md",
}


@dataclass(frozen=True)
class ExtractedDocument:
    text: str
    page_starts: tuple[int, ...]
    page_count: int | None
    ocr_required: bool = False


def safe_filename(filename: str) -> tuple[str, DocumentKind]:
    """Accept only a leaf filename; never use a client path for storage."""
    if (
        not filename
        or len(filename) > 255
        or filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or "\x00" in filename
    ):
        raise MatterError("Document filename is invalid.")
    suffix = PurePath(filename).suffix.lower()
    kind = ALLOWED_EXTENSIONS.get(suffix)
    if kind is None:
        raise MatterError("Document format is not supported.")
    return filename, kind


def extract_document(filename: str, content: bytes) -> ExtractedDocument:
    _, kind = safe_filename(filename)
    if not content or len(content) > MAX_UPLOAD_BYTES:
        raise MatterError("Document is empty or exceeds the 10 MB limit.")
    if kind in {"txt", "md"}:
        try:
            return ExtractedDocument(content.decode("utf-8-sig"), (0,), None)
        except UnicodeDecodeError:
            raise MatterError("Text document must be UTF-8 encoded.") from None
    if kind == "docx":
        if not content.startswith(b"PK"):
            raise MatterError("DOCX document is malformed.")
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                info = archive.getinfo("word/document.xml")
                if info.file_size > MAX_UPLOAD_BYTES:
                    raise MatterError("DOCX extracted text exceeds the size limit.")
                xml = archive.read(info)
            if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
                raise MatterError("DOCX document is malformed.")
            root = ElementTree.fromstring(xml)
            word_ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
            ns = {"w": word_ns}
            paragraphs = []
            for paragraph in root.findall(".//w:p", ns):
                pieces = []
                for node in paragraph.iter():
                    if node.tag == f"{{{word_ns}}}t":
                        pieces.append(node.text or "")
                    elif node.tag == f"{{{word_ns}}}tab":
                        pieces.append("\t")
                    elif node.tag in {f"{{{word_ns}}}br", f"{{{word_ns}}}cr"}:
                        pieces.append("\n")
                if pieces:
                    paragraphs.append("".join(pieces))
            return ExtractedDocument("\n\n".join(paragraphs), (0,), None)
        except (KeyError, zipfile.BadZipFile, ElementTree.ParseError, RuntimeError):
            raise MatterError("DOCX document is malformed.") from None
    if not content.startswith(b"%PDF-"):
        raise MatterError("PDF document is malformed.")
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content), strict=False)
        if len(reader.pages) > 500:
            raise MatterError("PDF has too many pages.")
        pages = [page.extract_text() or "" for page in reader.pages]
    except MatterError:
        raise
    except Exception:
        raise MatterError("PDF text extraction failed.") from None
    starts = []
    offset = 0
    for page in pages:
        starts.append(offset)
        offset += len(page) + 2
    text = "\n\n".join(pages)
    if len(text) > MAX_UPLOAD_BYTES:
        raise MatterError("PDF extracted text exceeds the size limit.")
    return ExtractedDocument(
        text=text,
        page_starts=tuple(starts),
        page_count=len(pages),
        ocr_required=sum(char.isalnum() for char in text) < 20,
    )


def segment_document(
    document_id: str, content_hash: str, extracted: ExtractedDocument
) -> list[DocumentSection]:
    sections: list[DocumentSection] = []
    for match in re.finditer(r"[^\n]+(?:\n(?!\n)[^\n]+)*", extracted.text):
        raw = match.group()
        if not raw.strip():
            continue
        start = match.start() + len(raw) - len(raw.lstrip())
        end = match.end() - len(raw) + len(raw.rstrip())
        if end <= start:
            continue
        page = (
            sum(origin <= start for origin in extracted.page_starts)
            if extracted.page_count is not None
            else None
        )
        section_id = hashlib.sha256(
            f"{content_hash}:{start}:{end}".encode()
        ).hexdigest()[:24]
        text = extracted.text[start:end]
        is_heading = bool(re.fullmatch(r"(?:[IVX]+\.|[A-Z][A-Z\s]{3,})[^.]*", text))
        sections.append(
            DocumentSection(
                section_id=section_id,
                document_id=document_id,
                order=len(sections),
                kind="heading" if is_heading else "paragraph",
                text=text,
                span=SourceSpan(
                    document_id=document_id,
                    section_id=section_id,
                    start=start,
                    end=end,
                    page=page,
                ),
            )
        )
    return sections

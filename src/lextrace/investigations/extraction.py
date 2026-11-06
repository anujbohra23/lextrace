"""Build manifests from the existing parser's exact, unmodified result."""

import hashlib
import json
import platform
from importlib.metadata import version
from typing import Literal

from lextrace.investigations.contracts import DocumentVersion
from lextrace.matter.contracts import MatterDocument, MatterError
from lextrace.matter.documents import ExtractedDocument


def document_version(
    document: MatterDocument, content: bytes, extracted: ExtractedDocument
) -> DocumentVersion:
    """Identity covers parser dependencies, settings, text and coverage metadata.

    Version 1 identifies the matter/documents.py extraction algorithm. Bump it
    whenever decoding, paragraph assembly, page joining or coverage changes.
    PDF/DOCX coverage is deliberately partial: these parsers cannot attest that
    every image, annotation, header, footnote or other source object was read.
    """
    try:
        text_bytes = extracted.text.encode("utf-8")
    except UnicodeEncodeError:
        raise MatterError("Extracted document contains invalid Unicode.") from None
    if hashlib.sha256(content).hexdigest() != document.content_hash:
        raise MatterError("Document source hash does not match extraction.")
    kind = document.document_type
    parser = f"lextrace.matter.documents/{kind}"
    parser_version = f"1;python={platform.python_version()}"
    settings: dict[str, object] = {"algorithm": 1, "kind": kind}
    warnings: tuple[str, ...] = ()
    coverage: Literal["COMPLETE", "PARTIAL", "OCR_REQUIRED"] = "COMPLETE"
    if kind == "pdf":
        parser_version += f";pypdf={version('pypdf')}"
        settings.update(strict=False, page_separator="\n\n", ocr_alnum_threshold=20)
        warnings = (
            "PDF extraction covers text only; full page coverage is unverified.",
        )
        coverage = "PARTIAL"
    elif kind == "docx":
        settings.update(part="word/document.xml", paragraph_separator="\n\n")
        warnings = ("DOCX extraction covers body paragraphs only; pagination unknown.",)
        coverage = "PARTIAL"
    else:
        settings.update(encoding="utf-8-sig")
    if extracted.ocr_required:
        coverage = "OCR_REQUIRED"
        warnings += ("Insufficient extractable text; OCR is required.",)
    manifest = DocumentVersion(
        matter_id=document.matter_id,
        document_id=document.document_id,
        version_id="pending",
        original_content_hash=document.content_hash,
        extracted_text_hash=hashlib.sha256(text_bytes).hexdigest(),
        parser=parser,
        parser_version=parser_version,
        extraction_settings_hash=hashlib.sha256(
            json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        document_type=kind,
        text_length=len(extracted.text),
        page_starts=extracted.page_starts if kind == "pdf" else (),
        extraction_warnings=warnings,
        coverage=coverage,
    )
    identity = manifest.model_dump(exclude={"version_id"})
    return manifest.model_copy(
        update={
            "version_id": hashlib.sha256(
                json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        }
    )

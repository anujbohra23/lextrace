"""Pure, fail-closed verification against a trusted source version and section."""

from hashlib import sha256
from typing import Literal

from pydantic import ValidationError

from lextrace.investigations.contracts import DocumentVersion, EvidenceReference
from lextrace.matter.contracts import DocumentSection

ValidationIssue = Literal[
    "MALFORMED_REFERENCE",
    "MALFORMED_VERSION",
    "MALFORMED_SECTION",
    "MATTER_MISMATCH",
    "DOCUMENT_MISMATCH",
    "VERSION_MISMATCH",
    "ORIGINAL_HASH_MISMATCH",
    "EXTRACTED_HASH_MISMATCH",
    "EXTRACTION_IDENTITY_MISMATCH",
    "TEXT_LENGTH_MISMATCH",
    "SECTION_MISMATCH",
    "SECTION_BOUNDS_INVALID",
    "SECTION_TEXT_MISMATCH",
    "SPAN_BOUNDS_INVALID",
    "SECTION_CONTAINMENT_MISMATCH",
    "QUOTE_MISMATCH",
    "PAGE_MISMATCH",
    "INVALID_UNICODE",
]


def validate_evidence_reference(
    reference: EvidenceReference,
    version: DocumentVersion,
    section: DocumentSection,
    extracted_text: str,
) -> tuple[ValidationIssue, ...]:
    """Return ordered safe issue codes; an empty tuple means exact provenance.

    The caller must resolve the manifest and section through trusted matter-scoped
    storage. Original bytes are unavailable here: their hash and parser identity
    are checked against that manifest; the extracted hash is also recomputed.
    No normalization, searching for another occurrence, or source repair occurs.
    """
    # Revalidate dumps because model_copy/model_construct bypass Pydantic checks.
    checks: tuple[
        tuple[EvidenceReference | DocumentVersion | DocumentSection, ValidationIssue],
        ...,
    ] = (
        (reference, "MALFORMED_REFERENCE"),
        (version, "MALFORMED_VERSION"),
        (section, "MALFORMED_SECTION"),
    )
    for model, issue in checks:
        try:
            type(model).model_validate(model.model_dump(), strict=True)
        except ValidationError:
            return (issue,)

    issues: list[ValidationIssue] = []
    if reference.matter_id != version.matter_id:
        issues.append("MATTER_MISMATCH")
    if (
        reference.document_id != version.document_id
        or section.document_id != version.document_id
        or section.span.document_id != version.document_id
    ):
        issues.append("DOCUMENT_MISMATCH")
    if reference.version_id != version.version_id:
        issues.append("VERSION_MISMATCH")
    if reference.original_content_hash != version.original_content_hash:
        issues.append("ORIGINAL_HASH_MISMATCH")
    try:
        text_hash = sha256(extracted_text.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        return ("INVALID_UNICODE",)
    if (
        reference.extracted_text_hash != version.extracted_text_hash
        or text_hash != version.extracted_text_hash
    ):
        issues.append("EXTRACTED_HASH_MISMATCH")
    if (
        reference.parser != version.parser
        or reference.parser_version != version.parser_version
        or reference.extraction_settings_hash != version.extraction_settings_hash
    ):
        issues.append("EXTRACTION_IDENTITY_MISMATCH")
    if len(extracted_text) != version.text_length:
        issues.append("TEXT_LENGTH_MISMATCH")
    if (
        reference.section_id != section.section_id
        or section.span.section_id != section.section_id
    ):
        issues.append("SECTION_MISMATCH")
    if not 0 <= section.span.start < section.span.end <= len(extracted_text):
        issues.append("SECTION_BOUNDS_INVALID")
    if extracted_text[section.span.start : section.span.end] != section.text:
        issues.append("SECTION_TEXT_MISMATCH")
    if not 0 <= reference.start < reference.end <= len(extracted_text):
        issues.append("SPAN_BOUNDS_INVALID")
    if not section.span.start <= reference.start < reference.end <= section.span.end:
        issues.append("SECTION_CONTAINMENT_MISMATCH")
    if extracted_text[reference.start : reference.end] != reference.exact_quote:
        issues.append("QUOTE_MISMATCH")
    if reference.page_start is not None:
        if not version.page_starts or (
            reference.page_start
            != sum(start <= reference.start for start in version.page_starts)
            or reference.page_end
            != sum(start < reference.end for start in version.page_starts)
        ):
            issues.append("PAGE_MISMATCH")
    return tuple(issues)

"""Immutable source identity and exact Unicode evidence contracts."""

from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, model_validator

from lextrace.matter.contracts import DocumentKind
from lextrace.retrieval.contracts import Record

Identifier = Annotated[str, Field(min_length=1, pattern=r"\S")]
SHA256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Offset = Annotated[int, Field(ge=0)]
Page = Annotated[int, Field(gt=0)]


class SourceIdentity(Record):
    """Identity supplied by a trusted version manifest, never inferred from a quote."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    matter_id: Identifier
    document_id: Identifier
    version_id: Identifier
    original_content_hash: SHA256
    extracted_text_hash: SHA256
    parser: Identifier
    parser_version: Identifier
    extraction_settings_hash: SHA256


class DocumentVersion(SourceIdentity):
    """Immutable manifest; hashes are SHA-256, text uses unmodified UTF-8.

    Version IDs are opaque and must change whenever the representation changes.
    Page starts are full-text code-point offsets for PDF pages only. Repeated
    starts are allowed for empty pages. An empty tuple means pagination is unknown.
    """

    document_type: DocumentKind
    text_length: Offset
    page_starts: tuple[Offset, ...] = ()
    extraction_warnings: tuple[str, ...] = ()
    coverage: Literal["COMPLETE", "PARTIAL", "OCR_REQUIRED"]

    @model_validator(mode="after")
    def valid_pages(self) -> Self:
        if self.page_starts and (
            self.document_type != "pdf"
            or self.page_starts[0] != 0
            or tuple(sorted(self.page_starts)) != self.page_starts
            or self.page_starts[-1] > self.text_length
        ):
            raise ValueError("Invalid source pagination.")
        return self


class EvidenceReference(SourceIdentity):
    """Exact half-open Python Unicode span into one immutable full-text version."""

    section_id: Identifier
    start: Offset
    end: Offset
    exact_quote: Annotated[str, Field(min_length=1)]
    page_start: Page | None = None
    page_end: Page | None = None

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("Evidence span must have positive length.")
        if (self.page_start is None) != (self.page_end is None) or (
            self.page_start is not None
            and self.page_end is not None
            and self.page_end < self.page_start
        ):
            raise ValueError("Invalid source page range.")
        return self

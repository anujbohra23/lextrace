"""Immutable source identity and exact Unicode evidence contracts."""

from datetime import date
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

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


class GeneratedRecord(Record):
    """Server-issued stable identity; edits retain ID and create a new revision."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    matter_id: Identifier
    document_id: Identifier
    version_id: Identifier
    run_id: Identifier
    schema_version: Literal[1] = 1
    revision: Annotated[int, Field(gt=0)] = 1
    created_at: AwareDatetime
    generator: Identifier


class SourcedRecord(GeneratedRecord):
    """Document-local candidate, not proof of provenance or semantic truth.

    References must still pass validate_evidence_reference against trusted storage.
    Cross-document synthesis belongs in downstream records, not extraction records.
    """

    evidence_references: Annotated[tuple[EvidenceReference, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def consistent_sources(self) -> Self:
        identities: set[tuple[str, str, str, str, str]] = set()
        for ref in self.evidence_references:
            if (ref.matter_id, ref.document_id, ref.version_id) != (
                self.matter_id,
                self.document_id,
                self.version_id,
            ):
                raise ValueError(
                    "Evidence ownership differs from extraction ownership."
                )
            identities.add(
                (
                    ref.original_content_hash,
                    ref.extracted_text_hash,
                    ref.parser,
                    ref.parser_version,
                    ref.extraction_settings_hash,
                )
            )
        if len(identities) != 1:
            raise ValueError("Conflicting source representation identities.")
        return self


class ReviewState(StrEnum):
    PROPOSED = "PROPOSED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    DEFERRED = "DEFERRED"
    STALE = "STALE"


class InterpretationBasis(StrEnum):
    SOURCE_ASSERTION = "SOURCE_ASSERTION"
    INFERRED_INTERPRETATION = "INFERRED_INTERPRETATION"
    REVIEWED_FINDING = "REVIEWED_FINDING"


class ReviewedRecord(SourcedRecord):
    interpretation_basis: InterpretationBasis = InterpretationBasis.SOURCE_ASSERTION
    verification_state: Literal["UNVERIFIED", "VALID", "INVALID", "INSUFFICIENT"] = (
        "UNVERIFIED"
    )
    review_state: ReviewState = ReviewState.PROPOSED
    review_decision_id: Identifier | None = None

    @model_validator(mode="after")
    def reviewed_basis(self) -> Self:
        if (
            self.review_state != ReviewState.PROPOSED
            and self.review_decision_id is None
        ):
            raise ValueError("Reviewed records require a review decision reference.")
        if self.interpretation_basis == InterpretationBasis.REVIEWED_FINDING and (
            self.review_state != ReviewState.ACCEPTED or self.review_decision_id is None
        ):
            raise ValueError("Reviewed findings require an accepted review decision.")
        return self


class EntityMention(SourcedRecord):
    """An explicit mention, not an inferred identity merge or canonical entity."""

    mention_id: Identifier
    entity_type: Literal["person", "organization", "location", "other"]
    mention_text: Identifier
    entity_id: Identifier | None = None

    @model_validator(mode="after")
    def explicit_mention(self) -> Self:
        if any(
            ref.exact_quote != self.mention_text for ref in self.evidence_references
        ):
            raise ValueError("Mention references must quote the exact mention text.")
        return self


class TemporalReference(ReviewedRecord):
    """Raw expression plus optional inclusive date bounds; no guessed dates.

    Partial dates use approximate bounds and precision, never a fabricated day.
    Unresolved relative expressions have no bounds. Timezones are source-supplied.
    """

    temporal_id: Identifier
    raw_expression: Identifier | None = None
    kind: Literal["exact", "approximate", "relative", "interval", "unknown"]
    earliest: date | None = None
    latest: date | None = None
    precision: Literal["day", "month", "year", "unknown"] = "unknown"
    timezone: Identifier | None = None

    @model_validator(mode="after")
    def valid_time(self) -> Self:
        if (self.earliest is None) != (self.latest is None):
            raise ValueError("Temporal bounds must both be supplied or both absent.")
        if self.earliest is not None and self.latest is not None:
            if self.latest < self.earliest:
                raise ValueError("Temporal bounds must be ordered.")
        if self.kind in ("unknown", "relative") and (
            self.earliest is not None or self.precision != "unknown"
        ):
            raise ValueError("Unknown and unresolved relative dates have no bounds.")
        if self.kind != "unknown" and self.raw_expression is None:
            raise ValueError("A stated time requires its raw expression.")
        if self.kind in ("exact", "interval") and self.earliest is None:
            raise ValueError("Exact dates and intervals require bounds.")
        if self.kind == "exact" and (
            self.earliest != self.latest or self.precision != "day"
        ):
            raise ValueError("Exact dates require one day with day precision.")
        return self


class AttributedAssertion(ReviewedRecord):
    """A normalized source account; VALID provenance does not establish truth."""

    assertion_id: Identifier
    normalized_proposition: Identifier
    attributed_to: Identifier | None = None
    attribution_status: Literal["known", "unknown", "unresolved"]
    subject: Identifier | None = None
    predicate: Identifier | None = None
    object: Identifier | None = None
    polarity: Literal["positive", "negative", "unknown"] = "unknown"
    modality: Literal["asserted", "alleged", "possible", "conditional", "unknown"]
    uncertainty: Identifier | None = None
    dispute_status: Literal["unresolved", "disputed", "undisputed"] = "unresolved"
    stated_time: TemporalReference | None = None

    @model_validator(mode="after")
    def valid_attribution(self) -> Self:
        if (self.attribution_status == "known") != (self.attributed_to is not None):
            raise ValueError("Known attribution requires a source or speaker.")
        if any(v is not None for v in (self.subject, self.predicate, self.object)) and (
            self.subject is None or self.predicate is None or self.object is None
        ):
            raise ValueError("Supply a full subject/predicate/object or text fallback.")
        if self.stated_time is not None:
            _same_extraction(self, self.stated_time)
        return self


class EventParticipant(Record):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    entity_id: Identifier
    role: Identifier


class InvestigationEvent(ReviewedRecord):
    """One account of an occurrence; separate accounts need not be merged."""

    event_id: Identifier
    description: Identifier
    participants: tuple[EventParticipant, ...] = ()
    fact_ids: tuple[Identifier, ...] = ()
    temporal: TemporalReference
    uncertainty: Identifier | None = None

    @model_validator(mode="after")
    def consistent_time(self) -> Self:
        _same_extraction(self, self.temporal)
        return self


def _same_extraction(parent: GeneratedRecord, child: GeneratedRecord) -> None:
    if (parent.matter_id, parent.document_id, parent.version_id, parent.run_id) != (
        child.matter_id,
        child.document_id,
        child.version_id,
        child.run_id,
    ):
        raise ValueError("Nested record belongs to another extraction input.")


class ExtractionSpan(Record):
    """Half-open full-text window, optionally located on source PDF pages."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    start: Offset
    end: Offset
    page_start: Page | None = None
    page_end: Page | None = None

    @model_validator(mode="after")
    def ordered_span(self) -> Self:
        if self.end <= self.start:
            raise ValueError("Coverage span must have positive length.")
        if (self.page_start is None) != (self.page_end is None) or (
            self.page_start is not None
            and self.page_end is not None
            and self.page_end < self.page_start
        ):
            raise ValueError("Invalid coverage page range.")
        return self


class SkippedSpan(ExtractionSpan):
    reason: Identifier


class ExtractionCoverage(GeneratedRecord):
    """Per-version coverage. Empty OCR text does not mean successful extraction."""

    coverage_id: Identifier
    text_length: Offset
    status: Literal["COMPLETE", "PARTIAL", "OCR_REQUIRED"]
    processed_windows: tuple[ExtractionSpan, ...] = ()
    processed_pages: tuple[Page, ...] = ()
    skipped_spans: tuple[SkippedSpan, ...] = ()
    excluded_pages: tuple[Page, ...] = ()
    warnings: tuple[Identifier, ...] = ()
    incomplete_reasons: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def valid_coverage(self) -> Self:
        if set(self.processed_pages) & set(self.excluded_pages):
            raise ValueError("A page cannot be both processed and excluded.")
        for span in (*self.processed_windows, *self.skipped_spans):
            if span.end > self.text_length:
                raise ValueError("Coverage exceeds extracted text bounds.")
        for skipped in self.skipped_spans:
            if any(
                window.start < skipped.end and skipped.start < window.end
                for window in self.processed_windows
            ):
                raise ValueError("Processed and skipped spans overlap.")
        if self.status != "COMPLETE" and not self.incomplete_reasons:
            raise ValueError("Incomplete coverage requires explicit reasons.")
        if self.status == "OCR_REQUIRED" and (
            self.processed_windows or self.processed_pages
        ):
            raise ValueError("OCR-required input must be visibly excluded.")
        if self.status == "COMPLETE":
            if self.skipped_spans or self.excluded_pages or self.incomplete_reasons:
                raise ValueError("Complete coverage cannot contain exclusions.")
            end = 0
            for window in sorted(self.processed_windows, key=lambda span: span.start):
                if window.start > end:
                    raise ValueError("Complete coverage contains an unprocessed gap.")
                end = max(end, window.end)
            if end != self.text_length:
                raise ValueError("Complete coverage must cover all extracted text.")
        return self

"""Typed matter, document, provenance, and Argument X-Ray records."""

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import Field, model_validator

from lextrace.research.contracts import VerificationStatus
from lextrace.retrieval.contracts import Record, RetrievalResult

MatterStatus = Literal["OPEN", "ARCHIVED"]
DocumentStatus = Literal["READY", "OCR_REQUIRED", "FAILED"]
DocumentKind = Literal["pdf", "docx", "txt", "md"]
ResolutionStatus = Literal["RESOLVED", "AMBIGUOUS", "NOT_FOUND"]
FindingStatus = Literal[
    "STRONG", "MIXED", "VULNERABLE", "UNSUPPORTED", "INSUFFICIENT_EVIDENCE"
]
PositiveOffset = Annotated[int, Field(ge=0)]


class MatterError(Exception):
    """Safe matter operation error without private content or file paths."""


class Matter(Record):
    matter_id: str
    name: Annotated[str, Field(min_length=1, max_length=200)]
    court: str | None = None
    jurisdiction: str | None = None
    state: str | None = None
    as_of_date: date | None = None
    reference: str | None = None
    created_at: datetime
    updated_at: datetime
    status: MatterStatus = "OPEN"


class MatterDocument(Record):
    document_id: str
    matter_id: str
    filename: str
    document_type: DocumentKind
    content_hash: str
    ingestion_status: DocumentStatus
    page_count: int | None = None
    created_at: datetime
    text_length: int = 0
    analysis_warnings: list[str] = Field(default_factory=list)


class SourceSpan(Record):
    document_id: str
    section_id: str
    start: PositiveOffset
    end: PositiveOffset
    page: int | None = None

    @model_validator(mode="after")
    def ordered(self) -> "SourceSpan":
        if self.end <= self.start:
            raise ValueError("Source span must have positive length.")
        return self


class DocumentSection(Record):
    section_id: str
    document_id: str
    order: int
    kind: Literal["heading", "paragraph"]
    text: str
    span: SourceSpan


class MatterFact(Record):
    fact_id: str
    matter_id: str
    document_id: str
    exact_text: str
    span: SourceSpan


class LegalIssue(Record):
    issue_id: str
    matter_id: str
    label: str
    section_ids: list[str]
    uncertainty: str | None = None
    research_topics: list[str] = Field(default_factory=list)


class LegalClaim(Record):
    claim_id: str
    matter_id: str
    document_id: str
    issue_id: str | None = None
    exact_source_text: str
    normalized_proposition: str
    span: SourceSpan
    importance: Literal["high", "medium", "low"] = "medium"
    claim_type: str = "legal proposition"
    citation_ids: list[str] = Field(default_factory=list)
    verification_state: VerificationStatus = "INSUFFICIENT_EVIDENCE"
    irrelevant: bool = False
    manually_edited: bool = False
    wording_locked: bool = False
    reviewed: bool = False
    review_notes: str = Field(default="", max_length=4000)


class DocumentCitation(Record):
    citation_id: str
    matter_id: str
    document_id: str
    exact_text: str
    span: SourceSpan
    claim_ids: list[str] = Field(default_factory=list)
    parsed_case_name: str | None = None
    reporter_citation: str | None = None
    resolution_status: ResolutionStatus = "NOT_FOUND"
    resolved_case_id: str | None = None
    resolution_evidence: str | None = None


class MatterEvidence(Record):
    evidence_id: str
    claim_id: str
    role: Literal["cited", "independent_support", "counter"]
    retrieval_query: str
    retrieval_trace_id: str | None = None
    result: RetrievalResult

    @model_validator(mode="after")
    def courtlistener_source(self) -> "MatterEvidence":
        if (
            self.result.source_url.scheme != "https"
            or self.result.source_url.host != "www.courtlistener.com"
        ):
            raise ValueError("Evidence source must be a CourtListener URL.")
        return self


class ClaimAuthorityLink(Record):
    claim_id: str
    case_id: str
    relation: Literal[
        "CITES", "SUPPORTS", "PARTIALLY_SUPPORTS", "CONTRADICTS", "COUNTERS"
    ]
    evidence_id: str | None = None
    citation_id: str | None = None
    pinned: bool = False
    removed: bool = False


class CounterAuthority(Record):
    claim_id: str
    evidence: MatterEvidence
    research_query: str


class ArgumentFinding(Record):
    claim_id: str
    issue_id: str | None
    proposition: str
    source: SourceSpan
    cited_authorities: list[ClaimAuthorityLink]
    evidence: list[MatterEvidence]
    counter_authorities: list[CounterAuthority]
    unresolved_citation_ids: list[str]
    verification_status: VerificationStatus
    research_coverage: Literal["SEARCHED", "NO_RESULTS", "NOT_SEARCHED"]
    vulnerability: FindingStatus
    explanation: str
    warnings: list[str] = Field(default_factory=list)


class MatterAnalysis(Record):
    matter_id: str
    document_id: str
    issues: list[LegalIssue]
    claims: list[LegalClaim]
    citations: list[DocumentCitation]
    findings: list[ArgumentFinding]
    warnings: list[str] = Field(default_factory=list)

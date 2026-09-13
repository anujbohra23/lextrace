"""Product-facing precedent, treatment, and doctrine records."""

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import Field, HttpUrl, model_validator

from lextrace.graph.contracts import CitationEdge, GraphNode
from lextrace.graph.courts import AuthorityRelationship
from lextrace.retrieval.contracts import Passage, Record

TreatmentLabel = Literal[
    "FOLLOWS",
    "APPLIES",
    "RELIES_ON",
    "DISTINGUISHES",
    "LIMITS",
    "CRITICIZES",
    "OVERRULES",
    "CITES",
    "UNKNOWN",
]
ReviewState = Literal["UNREVIEWED", "CONFIRMED", "REJECTED", "UNCERTAIN"]
EventCategory = Literal[
    "RULE_ARTICULATED",
    "RULE_ADOPTED",
    "RULE_APPLIED",
    "RULE_EXPANDED",
    "RULE_LIMITED",
    "RULE_DISTINGUISHED",
    "RULE_OVERRULED",
    "RULE_CLARIFIED",
]
ImpactCategory = Literal[
    "STRENGTHENS",
    "WEAKENS",
    "MIXED",
    "NO_MATERIAL_EFFECT",
    "INSUFFICIENT_EVIDENCE",
]


class CitationContext(Record):
    citing_case_id: str
    cited_case_id: str
    citing_opinion_id: str
    passage: Passage
    source_url: HttpUrl
    matched_text: str
    confidence: Literal["EXACT_REPORTER", "EXACT_NAME"]

    @model_validator(mode="after")
    def courtlistener_source(self) -> "CitationContext":
        if (
            self.source_url.scheme != "https"
            or self.source_url.host != "www.courtlistener.com"
        ):
            raise ValueError("Citation context source must be CourtListener.")
        return self


class TreatmentAnnotation(Record):
    annotation_id: str
    graph_id: str
    citing_case_id: str
    cited_case_id: str
    generated_label: TreatmentLabel
    passage: CitationContext | None = None
    method: Literal["explicit-language-v1", "citation-only-v1"]
    model_version: str | None = None
    prompt_version: str | None = None
    confidence: Literal["HIGH", "LOW", "NONE"]
    review_requirement: Literal["AUTO_CONFIRMED", "REVIEW_REQUIRED"]
    review_state: ReviewState = "UNREVIEWED"
    reviewed_at: datetime | None = None
    created_at: datetime

    @model_validator(mode="after")
    def evidence_for_specific_label(self) -> "TreatmentAnnotation":
        if self.generated_label not in ("CITES", "UNKNOWN") and self.passage is None:
            raise ValueError("Specific treatment requires exact citation context.")
        if (
            self.generated_label == "OVERRULES"
            and self.review_requirement != "REVIEW_REQUIRED"
        ):
            raise ValueError("Overruling language requires human review.")
        return self


class TraceNode(Record):
    node: GraphNode
    authority: AuthorityRelationship
    relevant_passage: Passage | None = None
    relevance_score: float | None = None
    relevance_method: Literal["lexical-evidence-v1"] | None = None


class TraceEdge(Record):
    edge: CitationEdge
    context: CitationContext | None = None
    treatment: TreatmentAnnotation


class PrecedentTrace(Record):
    seed_case_id: str
    proposition: str | None = None
    as_of_date: date | None = None
    graph_id: str
    nodes: list[TraceNode]
    edges: list[TraceEdge]
    nodes_examined: int
    edges_examined: int
    context_recovered: int
    treatment_specific: int
    treatment_fallback: int
    elapsed_ms: int
    warnings: list[str] = Field(default_factory=list)


class DoctrineEvent(Record):
    event_id: str
    category: EventCategory
    case_id: str
    case_name: str | None
    court: str | None
    date_filed: date
    proposition: str
    supporting_passage: CitationContext
    annotation_id: str
    uncertainty: list[str] = Field(default_factory=list)


class GroundedStatement(Record):
    text: str
    annotation_ids: Annotated[list[str], Field(min_length=1)]


class DoctrineState(Record):
    proposition: str
    relevant_authority_ids: list[str]
    controlling_authority_ids: list[str]
    latest_relevant_case_id: str | None
    supporting_annotation_ids: list[str]
    limiting_annotation_ids: list[str]
    contrary_annotation_ids: list[str]
    unresolved_conflicts: list[str]
    synthesis: list[GroundedStatement]
    coverage_warning: str


class ArgumentImpact(Record):
    claim_id: str
    category: ImpactCategory
    explanation: str
    annotation_ids: list[str]
    research_gaps: list[str]


class DoctrineAnalysis(Record):
    claim_id: str
    trace: PrecedentTrace
    events: list[DoctrineEvent]
    state: DoctrineState
    impact: ArgumentImpact
    cache_hit: bool = False


class ClaimAuthorityAnalysis(Record):
    matter_id: str
    claim_id: str
    forum_court: str | None
    relationships: list[AuthorityRelationship]
    warning: str

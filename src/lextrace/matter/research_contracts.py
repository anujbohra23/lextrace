"""Reviewable, corpus-bounded Matter research and adversarial findings."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field

from lextrace.matter.contracts import SourceSpan
from lextrace.retrieval.contracts import Record, RetrievalResult

CoverageCategory = Literal["SUFFICIENT", "PARTIAL", "WEAK", "INSUFFICIENT", "UNKNOWN"]
GapType = Literal[
    "NO_CONTROLLING_AUTHORITY",
    "NO_COUNTER_AUTHORITY",
    "UNRESOLVED_CITATION",
    "MISSING_LATER_AUTHORITY",
    "MISSING_EARLIER_PRECEDENT",
    "INSUFFICIENT_FACTUAL_ANALOGUE",
    "CONFLICTING_AUTHORITY",
    "MISSING_ELEMENT_RESEARCH",
    "INSUFFICIENT_EVIDENCE",
    "OUTDATED_AUTHORITY",
    "UNKNOWN",
]
QueryIntent = Literal[
    "DIRECT_SUPPORT",
    "CONTROLLING_AUTHORITY",
    "COUNTER_AUTHORITY",
    "FACTUAL_ANALOGUE",
    "LIMITING_AUTHORITY",
    "LATER_TREATMENT",
    "EARLIER_PRECEDENT",
    "ELEMENT_SPECIFIC",
]
AttackType = Literal[
    "NO_AUTHORITY_SUPPORT",
    "WEAK_AUTHORITY",
    "NON_CONTROLLING_AUTHORITY",
    "OUTDATED_AUTHORITY",
    "LIMITED_PRECEDENT",
    "DISTINGUISHABLE_FACTS",
    "CONTRARY_AUTHORITY",
    "MISSING_ELEMENT",
    "FACTUAL_CONTRADICTION",
    "CITATION_OVERSTATEMENT",
    "UNRESOLVED_CITATION",
    "RESEARCH_GAP",
    "PROCEDURAL_LIMITATION",
    "UNKNOWN",
]
Severity = Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"]
StopReason = Literal["GAPS_RESOLVED", "NO_NOVELTY", "LIMIT_REACHED", "STOPPED", "ERROR"]


class ResearchGap(Record):
    gap_id: str
    claim_id: str
    type: GapType
    explanation: str
    evidence_ids: list[str] = Field(default_factory=list)
    objective: str
    priority: Literal["HIGH", "MEDIUM", "LOW"]
    resolved: bool = False


class ResearchCoverage(Record):
    claim_id: str
    issue_id: str | None
    category: CoverageCategory
    cache_identity: str | None = None
    queries_executed: int = 0
    authorities_retrieved: int = 0
    controlling_authorities: int = 0
    persuasive_authorities: int = 0
    supporting_authorities: int = 0
    contrary_authorities: int = 0
    later_authorities: int = 0
    unresolved_citations: int = 0
    unavailable_authority_text: int = 0
    treatment_reviewed: bool = False
    doctrine_reviewed: bool = False
    temporal_coverage: bool = False
    gaps: list[ResearchGap] = Field(default_factory=list)
    warning: str = (
        "Coverage describes only the currently indexed corpus; it does not "
        "establish that all relevant law has been found."
    )


class ResearchStep(Record):
    step_id: str
    claim_id: str
    gap_id: str
    intent: QueryIntent
    query: Annotated[str, Field(min_length=3, max_length=4000)]
    objective: str
    retrieval_strategy: Literal["bm25", "dense", "hybrid", "reranked"] = "reranked"
    desired_authority_relationship: str | None = None
    desired_temporal_direction: Literal["earlier", "later", "any"] = "any"
    max_results: Annotated[int, Field(ge=1, le=20)] = 5
    priority: Literal["HIGH", "MEDIUM", "LOW"] = "MEDIUM"
    approved: bool = False


class DeepResearchPlan(Record):
    plan_id: str
    matter_id: str
    claim_id: str
    claim_fingerprint: str
    context_identity: str | None = None
    status: Literal["DRAFT", "APPROVED", "RUNNING", "COMPLETED", "STOPPED"] = "DRAFT"
    steps: list[ResearchStep]
    max_rounds: Annotated[int, Field(ge=1, le=3)] = 2
    max_queries_per_round: Annotated[int, Field(ge=1, le=6)] = 3
    max_total_results: Annotated[int, Field(ge=1, le=60)] = 30
    max_graph_expansions: Annotated[int, Field(ge=0, le=20)] = 0
    max_llm_calls: Annotated[int, Field(ge=0, le=10)] = 0
    max_tokens: Annotated[int, Field(ge=0, le=20000)] = 0
    max_duration_seconds: Annotated[int, Field(ge=1, le=300)] = 90


class ResearchDiscovery(Record):
    step_id: str
    gap_id: str
    intent: QueryIntent
    round_number: int
    result: RetrievalResult


class ResearchRound(Record):
    number: int
    queries: list[str]
    new_case_ids: list[str]
    new_passage_ids: list[str]
    duplicate_case_ids: list[str]
    resolved_gap_ids: list[str]
    elapsed_ms: int


class DeepResearchRun(Record):
    run_id: str
    plan_id: str
    matter_id: str
    claim_id: str
    status: Literal["queued", "running", "completed", "failed", "stopped"]
    stop_reason: StopReason | None = None
    rounds: list[ResearchRound] = Field(default_factory=list)
    discoveries: list[ResearchDiscovery] = Field(default_factory=list)
    coverage: ResearchCoverage | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    warning: str | None = None
    elapsed_ms: int = 0
    queries_executed: int = 0
    new_authorities: int = 0
    duplicate_authorities: int = 0
    gaps_resolved: int = 0
    cache_hits: int = 0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    attack_hypotheses: int = 0
    verified_attacks: int = 0
    unverified_attacks: int = 0


class AttackFinding(Record):
    attack_id: str
    claim_id: str
    attack_type: AttackType
    proposition: str
    explanation: str
    matter_evidence_ids: list[str] = Field(default_factory=list)
    matter_spans: list[SourceSpan] = Field(default_factory=list)
    authority_case_ids: list[str] = Field(default_factory=list)
    passage_ids: list[str] = Field(default_factory=list)
    doctrine_annotation_ids: list[str] = Field(default_factory=list)
    severity: Severity
    confidence: Literal["HIGH", "MEDIUM", "LOW"]
    remediation: str
    research_run_id: str
    verification_basis: Literal[
        "finding", "red_team_judgment", "matter_comparison", "reviewed_doctrine"
    ] = "finding"
    verified: bool = False
    verification_notes: list[str] = Field(default_factory=list)


class FactComparisonItem(Record):
    category: Literal["FACTUAL_CONTRADICTION", "DISTINGUISHABLE_FACTS", "NONE"]
    section_id: str
    exact_quote: str
    case_id: str | None = None
    passage_id: str | None = None
    explanation: str
    uncertainty: str


class FactComparison(Record):
    items: list[FactComparisonItem]


class AttackSurface(Record):
    matter_id: str
    findings: list[AttackFinding]
    warning: str = "Attack severity is not a litigation-outcome prediction."


class EvidenceMatrixRow(Record):
    claim_id: str
    issue_id: str | None
    issue: str
    claim: str
    document_id: str
    document_name: str
    source_start: int
    source_end: int
    matter_evidence_ids: list[str]
    cited_case_ids: list[str]
    cited_source_urls: list[str]
    citation_support: str
    authority_category: str
    strongest_support_case_id: str | None
    strongest_counter_case_id: str | None
    later_treatment: list[str]
    doctrine_state: str
    coverage: CoverageCategory
    gaps: list[GapType]
    attack_severity: Severity | None
    attack_ids: list[str]
    argument_status: str
    importance: str

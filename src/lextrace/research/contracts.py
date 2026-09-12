"""Typed public contracts for grounded legal research runs."""

from datetime import date
from typing import Annotated, Literal

from pydantic import Field

from lextrace.graph.contracts import CitationNeighbor
from lextrace.retrieval.contracts import Mode, Record, RetrievalResult


class ResearchError(Exception):
    """Safe workflow/provider error without prompt, evidence, or credential text."""


class LegalIssue(Record):
    issue_id: str
    label: str
    description: str
    legal_area: str
    priority: Literal["high", "medium", "low"]
    search_terms: list[str]
    uncertainty: str | None = None


class ResearchTask(Record):
    task_id: str
    issue_id: str
    query: str
    purpose: str
    preferred_courts: list[str] = Field(default_factory=list)
    filed_before: date | None = None
    retrieval_mode: Mode = "citation_reranked"
    target_results: Annotated[int, Field(strict=True, ge=1, le=20)] = 5


class EvidenceItem(Record):
    result: RetrievalResult
    task_ids: list[str]


class CaseAnalysis(Record):
    case_id: str
    relevant_issue_ids: list[str]
    relevant_passage_ids: Annotated[list[str], Field(min_length=1)]
    proposition_supported: str
    factual_relevance: str
    legal_relevance: str
    limitations: list[str] = Field(default_factory=list)
    uncertainty: str | None = None


class Claim(Record):
    claim_id: str
    text: str
    cited_case_ids: Annotated[list[str], Field(min_length=1)]
    cited_passage_ids: Annotated[list[str], Field(min_length=1)]
    substantive: bool = True


class Argument(Record):
    argument_id: str
    issue_id: str
    position: Literal["supporting", "opposing"]
    claims: list[Claim]
    supporting_case_ids: list[str]
    supporting_passage_ids: list[str]
    limitations: list[str] = Field(default_factory=list)
    confidence: Literal["HIGH", "MODERATE", "LOW", "INSUFFICIENT_EVIDENCE"]


class Memo(Record):
    title: str = "Legal Research Memorandum"
    question_presented: str
    issues_identified: list[str]
    relevant_authorities: list[str]
    analysis: list[str]
    supporting_arguments: list[str]
    counterarguments: list[str]
    uncertainties: list[str]
    research_conclusion: str
    conclusion_strength: Literal[
        "strong support", "moderate support", "mixed authority", "insufficient evidence"
    ]
    disclaimer: Literal[
        "Legal research and information; not a substitute for professional "
        "legal advice."
    ] = (
        "Legal research and information; not a substitute for professional "
        "legal advice."
    )


VerificationStatus = Literal[
    "SUPPORTED",
    "PARTIALLY_SUPPORTED",
    "UNSUPPORTED",
    "CONTRADICTED",
    "INSUFFICIENT_EVIDENCE",
]


class VerificationResult(Record):
    claim_id: str
    status: VerificationStatus
    support_score: Annotated[float, Field(ge=0, le=1)]
    supporting_passage_ids: list[str]
    explanation: str
    missing_support: list[str] = Field(default_factory=list)


class GroundingSummary(Record):
    total_substantive_claims: int
    supported: int
    partially_supported: int
    unsupported: int
    contradicted: int
    insufficient_evidence: int
    support_rate: float
    confidence: Literal["HIGH", "MODERATE", "LOW", "INSUFFICIENT_EVIDENCE"]


class Usage(Record):
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float | None = None


class ResearchTrace(Record):
    run_id: str
    workflow_version: Literal["research-v1"] = "research-v1"
    status: Literal["completed", "degraded", "failed"]
    nodes_executed: list[str]
    node_seconds: dict[str, float]
    retrieval_trace_ids: list[str]
    prompts: list[str]
    provider: str
    model: str
    tool_errors: list[str]
    retry_count: int
    evidence_count: int
    claims_generated: int
    claims_supported: int
    revision_count: int
    usage: Usage
    total_seconds: float


class ResearchRequest(Record):
    question: Annotated[str, Field(min_length=1, max_length=4000)]
    jurisdiction: str | None = None
    as_of_date: date | None = None
    max_cases: Annotated[int, Field(strict=True, ge=1, le=20)] = 8


class ResearchResponse(Record):
    run_id: str
    identified_issues: list[LegalIssue]
    research_plan: list[ResearchTask]
    relevant_cases: list[EvidenceItem]
    citation_relationships: list[CitationNeighbor]
    case_analyses: list[CaseAnalysis]
    supporting_arguments: list[Argument]
    opposing_arguments: list[Argument]
    final_memo: Memo | None
    claims: list[Claim]
    verification_results: list[VerificationResult]
    grounding_summary: GroundingSummary
    warnings: list[str] = Field(default_factory=list)
    trace: ResearchTrace


class IssueOutput(Record):
    issues: list[LegalIssue]


class PlanOutput(Record):
    tasks: list[ResearchTask]


class AnalysisOutput(Record):
    analyses: list[CaseAnalysis]


class ArgumentOutput(Record):
    arguments: list[Argument]


class SynthesisOutput(Record):
    memo: Memo


class ClaimsOutput(Record):
    claims: list[Claim]


class VerificationOutput(Record):
    results: list[VerificationResult]


class RevisionOutput(Record):
    memo: Memo
    claims: list[Claim]

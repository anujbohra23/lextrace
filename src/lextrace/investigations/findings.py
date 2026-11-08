"""Bounded M6B contracts. Interpretations are hypotheses, never factual truth.

No free-form model prose is published: factual premises are exact source accounts;
comparison dimensions and competing explanations use a closed vocabulary.
"""

from typing import Annotated, Literal

from pydantic import Field

from lextrace.investigations.contracts import SHA256, EvidenceReference, SourceIdentity
from lextrace.investigations.review import RecordDependency, ReviewContract

Status = Literal[
    "POTENTIAL_INCONSISTENCY", "INSUFFICIENT_EVIDENCE", "NO_ESTABLISHED_CONFLICT"
]
Dimension = Literal[
    "speaker",
    "recipient",
    "subject",
    "object",
    "event_scope",
    "modality",
    "negation",
    "time",
    "identity",
    "predicate",
]
Explanation = Literal[
    "DIFFERENT_RECIPIENTS",
    "OVERLAPPING_DATES",
    "UNCLEAR_CHRONOLOGY",
    "HEARSAY",
    "MISTAKEN_IDENTITY",
    "DIFFERENT_EVENT_DEFINITIONS",
    "SAME_EVENT_PARAPHRASE",
]
ALL_EXPLANATIONS: tuple[Explanation, ...] = (
    "DIFFERENT_RECIPIENTS",
    "OVERLAPPING_DATES",
    "UNCLEAR_CHRONOLOGY",
    "HEARSAY",
    "MISTAKEN_IDENTITY",
    "DIFFERENT_EVENT_DEFINITIONS",
    "SAME_EVENT_PARAPHRASE",
)
ALL_DIMENSIONS: tuple[Dimension, ...] = (
    "speaker",
    "recipient",
    "subject",
    "object",
    "event_scope",
    "modality",
    "negation",
    "time",
    "identity",
    "predicate",
)
EvidenceID = SHA256


class ContextCheck(ReviewContract):
    dimension: Dimension
    assessment: Literal["COMPARABLE", "DIFFERENT", "UNKNOWN"]
    evidence_ids: Annotated[tuple[EvidenceID, ...], Field(max_length=20)]


class AlternativeExplanation(ReviewContract):
    explanation: Explanation
    assessment: Literal["POSSIBLE", "SUPPORTED", "NOT_SUPPORTED", "UNKNOWN"]
    evidence_ids: Annotated[tuple[EvidenceID, ...], Field(max_length=20)]
    interpretation: Literal["UNVERIFIED_HYPOTHESIS"] = "UNVERIFIED_HYPOTHESIS"


class EvidenceAssessment(ReviewContract):
    evidence_id: EvidenceID
    role: Literal["SUPPORTING", "CONTRARY", "CONTEXT"]
    entailment: Literal["SUPPORTED", "UNSUPPORTED", "UNKNOWN"]


class AccountPremise(ReviewContract):
    side: Literal["left", "right"]
    dimension: Dimension
    entailment: Literal["SUPPORTED", "UNSUPPORTED", "UNKNOWN"]
    evidence_ids: Annotated[tuple[EvidenceID, ...], Field(min_length=1, max_length=20)]


class Hypothesis(ReviewContract):
    status: Status
    premises: Annotated[tuple[AccountPremise, ...], Field(max_length=20)]
    checks: Annotated[tuple[ContextCheck, ...], Field(max_length=10)]
    alternatives: Annotated[tuple[AlternativeExplanation, ...], Field(max_length=7)]
    evidence: Annotated[tuple[EvidenceAssessment, ...], Field(max_length=30)]
    interpretation: Literal["UNVERIFIED_HYPOTHESIS"] = "UNVERIFIED_HYPOTHESIS"


class SourceAccount(ReviewContract):
    reference: EvidenceReference
    basis: Literal["SOURCE_ATTRIBUTED_QUOTE"] = "SOURCE_ATTRIBUTED_QUOTE"
    provenance: Literal["VALID"] = "VALID"


class Finding(ReviewContract):
    finding_id: SHA256
    pair_id: SHA256
    status: Status
    accounts: tuple[SourceAccount, ...]
    hypothesis: Hypothesis
    independent_assessment: Hypothesis
    verification: Literal["INDEPENDENT_STRUCTURED_SUPPORT", "INSUFFICIENT"]
    # Independent semantic assessment remains fallible, separate from exact offsets.
    dependencies: tuple[RecordDependency, ...]
    review_state: Literal["PROPOSED"] = "PROPOSED"


class InvestigationBudgets(ReviewContract):
    max_pairs: Annotated[int, Field(ge=0, le=200)] = 20
    max_searches: Annotated[int, Field(ge=0, le=20)] = 20
    searches_per_pair: Annotated[int, Field(ge=0, le=2)] = 1
    max_passages: Annotated[int, Field(ge=1, le=20)] = 8
    max_source_characters: Annotated[int, Field(ge=0, le=2000000)] = 2000000
    max_input_characters: Annotated[int, Field(ge=0, le=100000)] = 20000
    max_model_calls: Annotated[int, Field(ge=0, le=400)] = 40
    max_alternatives: Annotated[int, Field(ge=0, le=7)] = 7


class InvestigationDiagnostic(ReviewContract):
    code: Literal[
        "BUDGET_EXHAUSTED",
        "INVALID_OR_STALE_INPUT",
        "SEARCH_FAILED",
        "MODEL_FAILED",
        "INVALID_CITATION",
        "INSUFFICIENT_SUPPORT",
        "CURRENCY_CHANGED",
    ]
    count: Annotated[int, Field(gt=0)]
    stage: Literal["reasoner", "verifier"] | None = None
    reason: (
        Literal[
            "provider_timeout",
            "provider_transport",
            "provider_http_error",
            "provider_error",
            "malformed_response",
            "empty_output",
            "truncated_output",
            "schema_or_json_validation",
            "invalid_schema",
        ]
        | None
    ) = None


class InvestigationResult(ReviewContract):
    sources: tuple[SourceIdentity, ...]
    findings: tuple[Finding, ...]
    diagnostics: tuple[InvestigationDiagnostic, ...]
    dependencies: tuple[RecordDependency, ...]
    searches: int
    model_calls: int
    partial: bool

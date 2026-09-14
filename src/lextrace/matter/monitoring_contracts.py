"""Corpus-bounded monitoring records; an event is not a legal conclusion."""

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import Field, model_validator

from lextrace.graph.intelligence_contracts import TreatmentAnnotation
from lextrace.retrieval.contracts import Passage, Record, RetrievalResult

TargetType = Literal["CLAIM", "AUTHORITY", "ISSUE", "DOCTRINE", "MATTER"]
EventType = Literal[
    "NEW_RELEVANT_AUTHORITY",
    "NEW_CONTROLLING_AUTHORITY",
    "NEW_SUPPORTING_AUTHORITY",
    "NEW_COUNTER_AUTHORITY",
    "NEW_CITING_AUTHORITY",
    "NEW_TREATMENT",
    "DOCTRINE_CHANGE",
    "RESEARCH_GAP_RESOLVED",
    "RESEARCH_GAP_CHANGED",
    "AUTHORITY_METADATA_CHANGED",
    "UNKNOWN",
]
ImpactCategory = Literal[
    "STRENGTHENS",
    "WEAKENS",
    "CREATES_CONFLICT",
    "RESOLVES_GAP",
    "NEW_RELEVANT_AUTHORITY",
    "NO_MATERIAL_EFFECT",
    "INSUFFICIENT_EVIDENCE",
]
AlertSeverity = Literal["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFORMATIONAL"]
ReviewState = Literal[
    "UNREAD", "REVIEWED", "DISMISSED", "PINNED", "SNOOZED", "NOT_RELEVANT", "APPLIED"
]


class MonitoringPolicy(Record):
    courts: list[str] = Field(default_factory=list)
    target_types: list[TargetType] = ["CLAIM", "AUTHORITY", "MATTER"]
    relevance_threshold: Annotated[float, Field(ge=0, le=1)] = 0.35
    treatment_labels: list[str] = Field(
        default_factory=lambda: ["LIMITS", "OVERRULES", "DISTINGUISHES"]
    )
    authority_categories: list[str] = Field(default_factory=list)
    filed_after: date | None = None
    filed_before: date | None = None
    max_candidates_per_target: Annotated[int, Field(ge=1, le=20)] = 5
    enabled_event_types: list[EventType] = [
        "NEW_RELEVANT_AUTHORITY",
        "NEW_CONTROLLING_AUTHORITY",
        "NEW_SUPPORTING_AUTHORITY",
        "NEW_COUNTER_AUTHORITY",
        "NEW_CITING_AUTHORITY",
        "NEW_TREATMENT",
        "RESEARCH_GAP_RESOLVED",
    ]

    @model_validator(mode="after")
    def ordered(self) -> "MonitoringPolicy":
        if (
            self.filed_after
            and self.filed_before
            and self.filed_after > self.filed_before
        ):
            raise ValueError("Monitoring filing-date range is invalid.")
        return self


class MonitoringTarget(Record):
    target_id: str
    matter_id: str
    target_type: TargetType
    target_reference_id: str
    enabled: bool = True
    automatic: bool = False
    created_at: datetime
    updated_at: datetime
    as_of_date: date | None = None
    monitoring_scope: MonitoringPolicy = Field(default_factory=MonitoringPolicy)
    last_checked_at: datetime | None = None
    last_corpus_version: str | None = None
    last_graph_version: str | None = None


class CorpusVersion(Record):
    corpus_id: str
    version: str
    document_count: int
    case_ids: list[str]
    build_timestamp: datetime
    content_hash: str
    index_identity: str | None = None
    graph_identity: str | None = None
    metadata_hashes: dict[str, str]
    text_hashes: dict[str, str]


class CorpusDelta(Record):
    old_version: str
    new_version: str
    added_case_ids: list[str]
    removed_case_ids: list[str]
    metadata_changed_case_ids: list[str]
    text_changed_case_ids: list[str]
    citation_edges_added: list[str] = Field(default_factory=list)
    citation_edges_removed: list[str] = Field(default_factory=list)


class MonitoringLimits(Record):
    max_new_cases: Annotated[int, Field(ge=1, le=100)] = 25
    max_matters: Annotated[int, Field(ge=1, le=50)] = 10
    max_candidates_per_target: Annotated[int, Field(ge=1, le=20)] = 5
    max_stage_two: Annotated[int, Field(ge=1, le=100)] = 30
    max_llm_calls: Annotated[int, Field(ge=0, le=10)] = 0
    max_tokens: Annotated[int, Field(ge=0, le=20000)] = 0
    max_runtime_seconds: Annotated[int, Field(ge=1, le=300)] = 90


class MonitoringRun(Record):
    run_id: str
    status: Literal["queued", "running", "completed", "failed"] = "queued"
    outcome: (
        Literal["ALERTS", "NO_MATERIAL_CHANGE", "LIMIT_REACHED", "FAILED"] | None
    ) = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    old_corpus: CorpusVersion
    new_corpus: CorpusVersion
    delta: CorpusDelta | None = None
    old_graph_version: str | None = None
    new_graph_version: str | None = None
    matter_ids: list[str] = Field(default_factory=list)
    matters_examined: int = 0
    targets_examined: int = 0
    new_cases_examined: int = 0
    stage_one_candidates: int = 0
    stage_two_analyses: int = 0
    events_created: int = 0
    verified_impacts: int = 0
    alerts_created: int = 0
    rejected_impacts: int = 0
    cache_hits: int = 0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    elapsed_ms: int = 0
    errors: list[str] = Field(default_factory=list)


class ChangeEvent(Record):
    event_id: str
    monitoring_run_id: str
    matter_id: str
    target_id: str
    event_type: EventType
    new_case_id: str
    affected_claim_ids: list[str]
    affected_case_ids: list[str]
    passage: Passage | None = None
    result: RetrievalResult | None = None
    citation_provenance_ids: list[str] = Field(default_factory=list)
    treatment: TreatmentAnnotation | None = None
    discovered_at: datetime
    verification_state: Literal["VERIFIED", "UNVERIFIED"] = "UNVERIFIED"


class ChangeImpact(Record):
    impact_id: str
    event_id: str
    matter_id: str
    claim_id: str
    previous_finding_status: str | None
    previous_coverage: str | None
    previous_doctrine: str | None
    new_doctrine: str | None
    category: ImpactCategory
    explanation: str
    affected_proposition: str
    new_case_id: str
    passage: Passage | None
    authority_relationship: str
    treatment_relationship: str | None = None
    confidence: Literal["HIGH", "MEDIUM", "LOW"]
    review_state: Literal["PENDING", "APPLIED", "REJECTED"] = "PENDING"
    created_at: datetime


class MatterAlert(Record):
    alert_id: str
    matter_id: str
    claim_id: str
    event_id: str
    impact_id: str
    new_case_id: str
    severity: AlertSeverity
    title: str
    explanation: str
    review_state: ReviewState = "UNREAD"
    version: int = 1
    created_at: datetime
    updated_at: datetime
    snoozed_until: date | None = None


class MonitoringOverview(Record):
    matter_id: str
    policy: MonitoringPolicy
    targets: list[MonitoringTarget]
    recent_runs: list[MonitoringRun]
    alert_count: int
    unread_alert_count: int

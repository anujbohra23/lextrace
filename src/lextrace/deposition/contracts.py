"""M9 wire/domain contracts. Evidence identities are server-owned and immutable."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field

from lextrace.investigations.contracts import SHA256, EvidenceReference
from lextrace.investigations.findings import Finding
from lextrace.investigations.review import (
    RecordDependency,
    ReviewContract,
    ReviewRevision,
)
from lextrace.investigations.workflow import HumanEntityLink

Identity = Annotated[str, Field(min_length=1, max_length=256)]
Text = Annotated[str, Field(min_length=1, max_length=2000, pattern=r"\S")]
Category = Literal[
    "FOUNDATION", "TIMELINE", "CLARIFICATION", "POTENTIAL_INCONSISTENCY", "FOLLOW_UP"
]
Action = Literal["REVISE", "APPROVE", "REJECT", "DEFER"]


class PlanError(Exception):
    def __init__(self, code: str, status: int = 409) -> None:
        self.code, self.status = code, status
        super().__init__(code)


class WitnessDossier(ReviewContract):
    witness_id: SHA256
    label: str
    identity_basis: Literal["SOURCE_ACCOUNT_ATTRIBUTION"] = "SOURCE_ACCOUNT_ATTRIBUTION"
    accounts: tuple[ReviewRevision, ...]
    related_participants: tuple[HumanEntityLink, ...]
    chronology: tuple[ReviewRevision, ...]
    findings: tuple[Finding, ...]
    uncertainties: tuple[str, ...]


class QuestionDraft(ReviewContract):
    question_id: SHA256
    witness_id: SHA256
    category: Category
    wording: Text
    rationale: Text
    evidence_ids: Annotated[tuple[SHA256, ...], Field(min_length=1, max_length=30)]
    order: Annotated[int, Field(ge=0, le=1000)]
    group: Annotated[str, Field(max_length=120)] = ""
    disposition: Literal["INCLUDE", "REJECT", "DEFER"] = "INCLUDE"


class EvidenceItem(ReviewContract):
    evidence_id: SHA256
    reference: EvidenceReference


class QuestionPolicy(ReviewContract):
    question_id: SHA256
    # At least one original reference from EACH account is required.
    required_groups: tuple[tuple[SHA256, ...], ...]


class PlanBasis(ReviewContract):
    plan_id: SHA256
    matter_id: Identity
    thread_id: Identity
    snapshot_id: SHA256
    artifact_digest: SHA256
    source_dependency_digest: SHA256
    review_dependency_digest: SHA256
    dependencies: tuple[RecordDependency, ...]
    coverage: tuple[str, ...]
    outcome: Literal["FULLY_SOURCED", "LIMITED", "BLOCKED"]
    limitations: tuple[str, ...]
    witnesses: Annotated[tuple[WitnessDossier, ...], Field(max_length=100)]
    evidence: tuple[EvidenceItem, ...]
    questions: Annotated[tuple[QuestionDraft, ...], Field(max_length=200)]
    policies: tuple[QuestionPolicy, ...]
    meaning: Literal["DEPOSITION_PREPARATION_NOT_INVESTIGATION_SIGNOFF"] = (
        "DEPOSITION_PREPARATION_NOT_INVESTIGATION_SIGNOFF"
    )


class PlanRevision(ReviewContract):
    plan_id: SHA256
    revision: Annotated[int, Field(ge=1)]
    status: Literal["DRAFT", "APPROVED", "REJECTED", "DEFERRED"]
    questions: tuple[QuestionDraft, ...]
    reviewer_id: Identity
    timestamp: AwareDatetime
    decision_id: Identity
    action: Literal["CREATE", "REVISE", "APPROVE", "REJECT", "DEFER"]
    acknowledges_limitations: bool = False


class PlanView(ReviewContract):
    basis: PlanBasis
    current: PlanRevision


class ReviewPlan(ReviewContract):
    decision_id: Identity
    expected_revision: Annotated[int, Field(ge=1)]
    action: Action
    questions: Annotated[tuple[QuestionDraft, ...], Field(max_length=200)]
    confirm: bool = False
    acknowledge_limitations: bool = False


class PlanSummary(ReviewContract):
    plan_id: SHA256
    snapshot_id: SHA256
    revision: int
    status: str
    outcome: Literal["FULLY_SOURCED", "LIMITED", "BLOCKED"]
    witness_count: int
    question_count: int

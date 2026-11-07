"""Pure, append-only human review; acceptance never establishes factual truth."""

from datetime import date, timedelta
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from lextrace.investigations.contracts import (
    AttributedAssertion,
    EntityMention,
    Identifier,
    InvestigationEvent,
    ReviewState,
    TemporalReference,
)
from lextrace.retrieval.contracts import Record

Candidate = InvestigationEvent | AttributedAssertion | EntityMention


class ReviewContract(Record):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ReviewTarget(ReviewContract):
    matter_id: Identifier
    document_id: Identifier
    version_id: Identifier
    run_id: Identifier
    kind: Literal["event", "assertion", "mention"]
    record_id: Identifier


def target_of(candidate: Candidate) -> ReviewTarget:
    kind: Literal["event", "assertion", "mention"]
    if isinstance(candidate, InvestigationEvent):
        kind, identity = "event", candidate.event_id
    elif isinstance(candidate, AttributedAssertion):
        kind, identity = "assertion", candidate.assertion_id
    else:
        kind, identity = "mention", candidate.mention_id
    return ReviewTarget(
        matter_id=candidate.matter_id,
        document_id=candidate.document_id,
        version_id=candidate.version_id,
        run_id=candidate.run_id,
        kind=kind,
        record_id=identity,
    )


class DateCorrection(ReviewContract):
    """Reviewer interpretation only; original TemporalReference stays untouched."""

    kind: Literal["exact", "approximate", "interval", "unknown"]
    earliest: date | None = None
    latest: date | None = None
    precision: Literal["day", "month", "year", "unknown"] = "unknown"

    @model_validator(mode="after")
    def valid_bounds(self) -> Self:
        # Reuse the established temporal invariants, without manufacturing evidence.
        if (self.earliest is None) != (self.latest is None):
            raise ValueError("Both date bounds are required.")
        if self.earliest is not None and self.latest is not None:
            if self.earliest > self.latest:
                raise ValueError("Date bounds are reversed.")
        if self.kind == "unknown" and (
            self.earliest is not None or self.precision != "unknown"
        ):
            raise ValueError("Unknown dates have no bounds.")
        if self.kind != "unknown" and self.earliest is None:
            raise ValueError("A correction requires explicit bounds.")
        if self.kind == "exact" and (
            self.earliest != self.latest or self.precision != "day"
        ):
            raise ValueError("Exact dates require one day.")
        return self


class ReviewDecision(ReviewContract):
    decision_id: Identifier
    reviewer_id: Identifier
    timestamp: AwareDatetime
    target: ReviewTarget
    expected_revision: Annotated[int, Field(gt=0)]
    decision: Literal["ACCEPT", "REJECT", "DEFER", "EDIT"]
    explanation: Identifier | None = None
    date_correction: DateCorrection | None = None
    resolved_entity_id: Identifier | None = None

    @model_validator(mode="after")
    def valid_command(self) -> Self:
        if self.timestamp.utcoffset() != timedelta(0):
            raise ValueError("Review timestamps must be UTC.")
        edits = self.date_correction is not None or self.resolved_entity_id is not None
        if (self.decision == "EDIT") != edits:
            raise ValueError("EDIT requires a correction; decisions cannot hide edits.")
        if self.date_correction is not None and self.target.kind == "mention":
            raise ValueError("Mentions cannot receive date corrections.")
        if self.resolved_entity_id is not None and self.target.kind != "mention":
            raise ValueError("Only explicit mentions can be resolved.")
        return self


class ReviewRevision(ReviewContract):
    original: Candidate
    revision: Annotated[int, Field(gt=0)] = 1
    review_state: ReviewState = ReviewState.PROPOSED
    decision: ReviewDecision | None = None
    date_correction: DateCorrection | None = None
    resolved_entity_id: Identifier | None = None

    @model_validator(mode="after")
    def valid_revision(self) -> Self:
        if self.decision is None:
            if self.revision != 1 or self.review_state != ReviewState.PROPOSED:
                raise ValueError("Initial revision must be proposed.")
            if self.date_correction is not None or self.resolved_entity_id is not None:
                raise ValueError("Initial revision cannot contain edits.")
        elif (
            self.decision.target != target_of(self.original)
            or self.decision.expected_revision + 1 != self.revision
        ):
            raise ValueError("Decision does not identify this revision.")
        if self.decision is not None:
            expected_state = {
                "ACCEPT": ReviewState.ACCEPTED,
                "REJECT": ReviewState.REJECTED,
                "DEFER": ReviewState.DEFERRED,
                "EDIT": ReviewState.PROPOSED,
            }[self.decision.decision]
            if self.review_state != expected_state:
                raise ValueError("Review state differs from its decision.")
        return self

    def interpreted_time(self) -> TemporalReference | DateCorrection | None:
        """Projection input retaining exact evidence and the original separately."""
        original = self.original
        temporal = (
            original.temporal
            if isinstance(original, InvestigationEvent)
            else original.stated_time
            if isinstance(original, AttributedAssertion)
            else None
        )
        if temporal is None or self.date_correction is None:
            return temporal
        return self.date_correction


def initial_review(candidate: Candidate) -> ReviewRevision:
    candidate = type(candidate).model_validate(candidate.model_dump(), strict=True)
    if candidate.revision != 1:
        raise ValueError("Expected original extraction revision.")
    if not isinstance(candidate, EntityMention) and (
        candidate.review_state != ReviewState.PROPOSED
        or candidate.review_decision_id is not None
    ):
        raise ValueError("Original extraction must be unreviewed.")
    return ReviewRevision(original=candidate)


def apply_review(current: ReviewRevision, command: ReviewDecision) -> ReviewRevision:
    """Any current state can be re-reviewed; edits reset acceptance to PROPOSED.

    Staleness is a repository publication gate, never a new human decision.
    """
    current = ReviewRevision.model_validate(current.model_dump(), strict=True)
    command = ReviewDecision.model_validate(command.model_dump(), strict=True)
    if (
        current.decision is not None
        and current.decision.decision_id == command.decision_id
    ):
        if current.decision == command:
            return current
        raise ValueError("Decision identity conflicts.")
    if command.target != target_of(current.original):
        raise ValueError("Review ownership mismatch.")
    if command.expected_revision != current.revision:
        raise ValueError("Review revision conflicts.")
    if command.date_correction is not None and current.interpreted_time() is None:
        raise ValueError("No original temporal reference exists.")
    state = {
        "ACCEPT": ReviewState.ACCEPTED,
        "REJECT": ReviewState.REJECTED,
        "DEFER": ReviewState.DEFERRED,
        "EDIT": ReviewState.PROPOSED,
    }[command.decision]
    return ReviewRevision(
        original=current.original,
        revision=current.revision + 1,
        review_state=state,
        decision=command,
        date_correction=command.date_correction or current.date_correction,
        resolved_entity_id=command.resolved_entity_id or current.resolved_entity_id,
    )


class RecordDependency(ReviewContract):
    target: ReviewTarget
    revision: Annotated[int, Field(gt=0)]


class ProjectionDependency(ReviewContract):
    """Flattened transitive record inputs for chronology and later artifacts.

    Later snapshot/pack services must include every upstream record revision;
    this contract creates no pack, workflow, finding, or export artifacts.
    """

    matter_id: Identifier
    projection_id: Identifier
    inputs: Annotated[tuple[RecordDependency, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def valid_inputs(self) -> Self:
        targets = [item.target for item in self.inputs]
        if any(target.matter_id != self.matter_id for target in targets):
            raise ValueError("Cross-matter dependency.")
        if len({target.model_dump_json() for target in targets}) != len(targets):
            raise ValueError("Duplicate dependency.")
        return self

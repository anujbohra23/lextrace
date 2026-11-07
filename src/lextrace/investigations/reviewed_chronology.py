"""Review-aware chronology with the M4A inclusive overlap ordering policy.

Callers supply current repository revisions, and persist their exact dependency
list with the projection. This pure builder cannot determine database currency.
Corrected dates are interpretations, never rewritten source temporal references.
"""

from dataclasses import dataclass
from datetime import date

from lextrace.investigations.contracts import InvestigationEvent
from lextrace.investigations.review import ReviewRevision, target_of


@dataclass(frozen=True)
class ReviewedChronologyGroup:
    earliest: date
    latest: date
    events: tuple[ReviewRevision, ...]


@dataclass(frozen=True)
class ReviewedChronology:
    matter_id: str
    dated_groups: tuple[ReviewedChronologyGroup, ...]
    unresolved_events: tuple[ReviewRevision, ...]
    excluded_events: tuple[ReviewRevision, ...]
    ordering_policy: str = "inclusive-overlap-components-v1"


def _identity(revision: ReviewRevision) -> tuple[str, str, str, str, str, int]:
    target = target_of(revision.original)
    return (
        target.matter_id,
        target.document_id,
        target.version_id,
        target.run_id,
        target.record_id,
        revision.revision,
    )


def build_reviewed_chronology(
    revisions: tuple[ReviewRevision, ...],
    *,
    matter_id: str,
    require_accepted: bool = False,
) -> ReviewedChronology:
    """Keep separate accounts, unknown dates, exact evidence and all exclusions.

    Provenance remains an independent gate even when the reviewer accepted a claim.
    Invalid or duplicate current revisions fail closed, rather than selecting one.
    """
    if not matter_id.strip():
        raise ValueError("Matter identity is required.")
    seen: dict[str, ReviewRevision] = {}
    dated: list[
        tuple[date, date, tuple[str, str, str, str, str, int], ReviewRevision]
    ] = []
    unknown: list[ReviewRevision] = []
    excluded: list[ReviewRevision] = []
    for revision in revisions:
        revision = ReviewRevision.model_validate(revision.model_dump(), strict=True)
        original = revision.original
        if original.matter_id != matter_id or not isinstance(
            original, InvestigationEvent
        ):
            raise ValueError("Chronology requires matter-owned events.")
        key = target_of(original).model_dump_json()
        if key in seen:
            if seen[key] != revision:
                raise ValueError("Conflicting current event revisions.")
            continue
        seen[key] = revision
        if (
            original.verification_state != "VALID"
            or original.temporal.verification_state != "VALID"
        ):
            raise ValueError("Event and temporal provenance must be VALID.")
        if revision.review_state in ("REJECTED", "DEFERRED", "STALE") or (
            require_accepted and revision.review_state != "ACCEPTED"
        ):
            excluded.append(revision)
            continue
        temporal = revision.interpreted_time()
        assert temporal is not None
        if temporal.earliest is None:
            unknown.append(revision)
        else:
            assert temporal.latest is not None
            dated.append(
                (temporal.earliest, temporal.latest, _identity(revision), revision)
            )
    groups: list[ReviewedChronologyGroup] = []
    for earliest, latest, _, revision in sorted(dated, key=lambda item: item[:3]):
        if groups and earliest <= groups[-1].latest:
            previous = groups[-1]
            groups[-1] = ReviewedChronologyGroup(
                previous.earliest,
                max(previous.latest, latest),
                (*previous.events, revision),
            )
        else:
            groups.append(ReviewedChronologyGroup(earliest, latest, (revision,)))
    return ReviewedChronology(
        matter_id,
        tuple(groups),
        tuple(sorted(unknown, key=_identity)),
        tuple(sorted(excluded, key=_identity)),
    )

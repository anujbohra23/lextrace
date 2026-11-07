"""Pure chronology projection of trusted M3C verification results.

The caller must supply independently verified results, not model output or cached
VALID flags. Structural validation here is a defensive boundary, not a second
provenance verifier. Inclusion never establishes truth or changes review state.
"""

from dataclasses import dataclass
from datetime import date

from lextrace.investigations.contracts import InvestigationEvent
from lextrace.investigations.extraction_verification import (
    CoverageVerification,
    ExtractionVerificationResult,
)


@dataclass(frozen=True, order=True)
class EventIdentity:
    matter_id: str
    document_id: str
    version_id: str
    run_id: str
    event_id: str
    revision: int


@dataclass(frozen=True, order=True)
class ChronologyDiagnostic:
    code: str
    matter_id: str
    document_id: str
    version_id: str
    run_id: str
    event: EventIdentity | None = None


@dataclass(frozen=True)
class ChronologyGroup:
    """Connected inclusive ranges; entries are display ordered, not sequenced.

    Groups are strictly temporally separated. Within a group, overlap may be
    transitive: membership does not assert that every pair directly overlaps.
    Even exact same-day accounts have no inferred within-day sequence.
    """

    earliest: date
    latest: date
    events: tuple[InvestigationEvent, ...]


@dataclass(frozen=True)
class Chronology:
    matter_id: str
    dated_groups: tuple[ChronologyGroup, ...]
    unresolved_events: tuple[InvestigationEvent, ...]
    coverage: tuple[CoverageVerification, ...]
    diagnostics: tuple[ChronologyDiagnostic, ...]
    ordering_policy: str = "inclusive-overlap-components-v1"

    # Empty output describes extraction results only, never absence of occurrences.
    empty_events_mean: str = "NO_VERIFIED_EVENT_CANDIDATES"


def _identity(event: InvestigationEvent) -> EventIdentity:
    return EventIdentity(
        event.matter_id,
        event.document_id,
        event.version_id,
        event.run_id,
        event.event_id,
        event.revision,
    )


def _date_key(event: InvestigationEvent) -> tuple[date, date, EventIdentity]:
    temporal = event.temporal
    assert temporal.earliest is not None and temporal.latest is not None
    return temporal.earliest, temporal.latest, _identity(event)


def _coverage_key(coverage: CoverageVerification) -> tuple[str, str, str, str]:
    return (
        coverage.original.model_dump_json(),
        coverage.status,
        repr(coverage.uncovered_spans),
        repr(coverage.issue_codes),
    )


def build_chronology(
    results: tuple[ExtractionVerificationResult, ...], *, matter_id: str
) -> Chronology:
    """Project only valid events; exclude wrong-matter inputs with safe codes.

    Identical scoped event revisions are idempotent. Conflicting payloads for the
    same identity fail the entire build, without exposing private source text.
    Revisions remain distinct; selecting current revisions belongs to M4B/M4C.
    Coverage is retained even when no events were extracted. Quarantine is never
    consulted for inclusion. No dates, source repairs or review decisions inferred.
    """
    if not matter_id.strip():
        raise ValueError("Expected matter identity is required.")
    events: dict[EventIdentity, InvestigationEvent] = {}
    diagnostics: set[ChronologyDiagnostic] = set()
    coverage_records: list[CoverageVerification] = []
    for result in results:
        if not isinstance(result, ExtractionVerificationResult):
            raise TypeError("Trusted extraction verification results are required.")
        coverage = result.coverage
        original = coverage.original
        if original.matter_id != matter_id:
            diagnostics.add(
                ChronologyDiagnostic(
                    "COVERAGE_MATTER_MISMATCH",
                    original.matter_id,
                    original.document_id,
                    original.version_id,
                    original.run_id,
                )
            )
        elif coverage not in coverage_records:
            coverage_records.append(coverage)
        for candidate in result.valid_candidates:
            if not isinstance(candidate, InvestigationEvent):
                continue
            event = InvestigationEvent.model_validate(
                candidate.model_dump(), strict=True
            )
            identity = _identity(event)
            if event.matter_id != matter_id:
                diagnostics.add(
                    ChronologyDiagnostic(
                        "EVENT_MATTER_MISMATCH",
                        event.matter_id,
                        event.document_id,
                        event.version_id,
                        event.run_id,
                        identity,
                    )
                )
                continue
            if (
                event.document_id != original.document_id
                or event.version_id != original.version_id
                or event.run_id != original.run_id
            ):
                raise ValueError(
                    "Event identity differs from its extraction verification."
                )
            if (
                event.verification_state != "VALID"
                or event.temporal.verification_state != "VALID"
            ):
                raise ValueError("Event and temporal provenance must be VALID.")
            if identity in events and events[identity] != event:
                raise ValueError("Conflicting duplicate event identity.")
            events[identity] = event

    dated = sorted(
        (event for event in events.values() if event.temporal.earliest is not None),
        key=_date_key,
    )
    groups: list[ChronologyGroup] = []
    for event in dated:
        earliest, latest, _ = _date_key(event)
        if groups and earliest <= groups[-1].latest:
            previous = groups[-1]
            groups[-1] = ChronologyGroup(
                previous.earliest,
                max(previous.latest, latest),
                (*previous.events, event),
            )
        else:
            groups.append(ChronologyGroup(earliest, latest, (event,)))
    return Chronology(
        matter_id=matter_id,
        dated_groups=tuple(groups),
        unresolved_events=tuple(
            sorted(
                (event for event in events.values() if event.temporal.earliest is None),
                key=_identity,
            )
        ),
        coverage=tuple(sorted(coverage_records, key=_coverage_key)),
        diagnostics=tuple(sorted(diagnostics)),
    )

"""Transactional review persistence and exact dependency currency checks.

Audit rows have no source-delete cascade. Publication always checks live ownership,
active version and exact revisions, so deleted sources cannot leave current refs.
"""

import sqlite3
import threading
from typing import TYPE_CHECKING

from lextrace.investigations.contracts import (
    AttributedAssertion,
    DocumentVersion,
    EntityMention,
    InvestigationEvent,
)
from lextrace.investigations.provenance import validate_evidence_reference
from lextrace.investigations.review import (
    Candidate,
    ProjectionDependency,
    ReviewDecision,
    ReviewRevision,
    ReviewTarget,
    apply_review,
    initial_review,
    target_of,
)
from lextrace.investigations.reviewed_chronology import (
    ReviewedChronology,
    build_reviewed_chronology,
)
from lextrace.matter.contracts import DocumentSection, MatterError

if TYPE_CHECKING:
    from lextrace.matter.store import MatterStore


def activate_source(connection: sqlite3.Connection, version: DocumentVersion) -> None:
    """Called only on first insertion of a version, inside its transaction.

    Opaque IDs cannot imply chronology. Newly registered representations supersede
    older ones; replaying an existing manifest never reactivates it.
    """
    if connection.execute(
        "SELECT 1 FROM investigation_retired_sources "
        "WHERE matter_id=? AND document_id=? AND version_id=?",
        (version.matter_id, version.document_id, version.version_id),
    ).fetchone():
        raise MatterError("Retired source identity cannot be reused.")
    connection.execute(
        "INSERT OR IGNORE INTO investigation_retired_sources "
        "SELECT matter_id,document_id,version_id FROM investigation_active_sources "
        "WHERE matter_id=? AND document_id=? AND version_id!=?",
        (version.matter_id, version.document_id, version.version_id),
    )
    connection.execute(
        "INSERT INTO investigation_active_sources VALUES (?,?,?) "
        "ON CONFLICT(matter_id,document_id) DO UPDATE SET "
        "version_id=excluded.version_id",
        (version.matter_id, version.document_id, version.version_id),
    )


class ReviewStorage:
    """Repository mixin using its existing connection and reentrant lock."""

    _source_store: "MatterStore | None"
    _db: sqlite3.Connection
    _lock: threading.RLock

    def _source_current(self, target: ReviewTarget) -> bool:
        return (
            self._db.execute(
                "SELECT 1 FROM investigation_active_sources a "
                "JOIN investigation_document_versions v ON v.matter_id=a.matter_id "
                "AND v.document_id=a.document_id AND v.version_id=a.version_id "
                "JOIN matter_documents d ON d.matter_id=a.matter_id AND "
                "d.id=a.document_id "
                "JOIN matters m ON m.id=d.matter_id "
                "WHERE a.matter_id=? AND a.document_id=? AND a.version_id=?",
                (target.matter_id, target.document_id, target.version_id),
            ).fetchone()
            is not None
        )

    def _latest(self, target: ReviewTarget) -> ReviewRevision | None:
        row = self._db.execute(
            "SELECT payload FROM investigation_review_revisions "
            "WHERE matter_id=? AND target=? ORDER BY revision DESC LIMIT 1",
            (target.matter_id, target.model_dump_json()),
        ).fetchone()
        return ReviewRevision.model_validate_json(row[0]) if row else None

    def _append(self, revision: ReviewRevision) -> None:
        target = target_of(revision.original)
        self._db.execute(
            "INSERT INTO investigation_review_revisions VALUES (?,?,?,?)",
            (
                target.matter_id,
                target.model_dump_json(),
                revision.revision,
                revision.model_dump_json(),
            ),
        )

    def register_review_candidate(self, candidate: Candidate) -> ReviewRevision:
        initial = initial_review(candidate)
        candidate = initial.original
        target = target_of(initial.original)
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                if not self._source_current(target):
                    raise MatterError(
                        "Source is missing or stale, or has "
                        "no unambiguous active version."
                    )
                row = self._db.execute(
                    "SELECT payload FROM investigation_document_versions "
                    "WHERE matter_id=? AND document_id=? AND version_id=?",
                    (target.matter_id, target.document_id, target.version_id),
                ).fetchone()
                version = DocumentVersion.model_validate_json(row[0])
                nested = (
                    candidate.temporal
                    if isinstance(candidate, InvestigationEvent)
                    else candidate.stated_time
                    if isinstance(candidate, AttributedAssertion)
                    else None
                )
                references = candidate.evidence_references + (
                    nested.evidence_references if nested is not None else ()
                )
                if self._source_store is None:
                    raise MatterError("Trusted source text store is required.")
                try:
                    text = self._source_store.document_text(
                        target.matter_id, target.document_id
                    )
                except (OSError, UnicodeError) as exc:
                    raise MatterError("Trusted source text is unavailable.") from exc
                for reference in references:
                    section_row = self._db.execute(
                        "SELECT payload FROM document_sections "
                        "WHERE document_id=? AND id=?",
                        (target.document_id, reference.section_id),
                    ).fetchone()
                    if section_row is None:
                        raise MatterError("Evidence section was not found.")
                    section = DocumentSection.model_validate_json(section_row[0])
                    issues = validate_evidence_reference(
                        reference, version, section, text
                    )
                    if issues:
                        raise MatterError(
                            "Evidence version conflicts or invalid provenance: "
                            + ", ".join(issues)
                        )
                current = self._latest(target)
                if current is None:
                    self._append(initial)
                elif current.original != initial.original:
                    raise MatterError("Candidate identity conflicts.")
                self._db.commit()
                return current or initial
            except Exception:
                self._db.rollback()
                raise

    def review(self, command: ReviewDecision) -> ReviewRevision:
        command = ReviewDecision.model_validate(command.model_dump(), strict=True)
        with self._lock:
            try:
                # Serializes read/check/append across separate repository instances.
                self._db.execute("BEGIN IMMEDIATE")
                result = self.review_in_transaction(command)
                self._db.commit()
                return result
            except Exception:
                self._db.rollback()
                raise

    def review_in_transaction(self, command: ReviewDecision) -> ReviewRevision:
        """Apply one command in a caller-owned batch; never commit or rollback."""
        if not self._db.in_transaction:
            raise RuntimeError("Review requires a transaction.")
        if not self._source_current(command.target):
            raise MatterError("Source is missing or stale.")
        prior = self._db.execute(
            "SELECT payload,revision,target FROM "
            "investigation_review_decisions "
            "WHERE matter_id=? AND decision_id=?",
            (command.target.matter_id, command.decision_id),
        ).fetchone()
        if prior:
            if ReviewDecision.model_validate_json(prior[0]) != command:
                raise MatterError("Decision identity conflicts.")
            row = self._db.execute(
                "SELECT payload FROM investigation_review_revisions "
                "WHERE matter_id=? AND target=? AND revision=?",
                (command.target.matter_id, prior[2], prior[1]),
            ).fetchone()
            result = ReviewRevision.model_validate_json(row[0])
        else:
            current = self._latest(command.target)
            if current is None:
                raise MatterError("Review target was not found.")
            try:
                result = apply_review(current, command)
            except ValueError as exc:
                raise MatterError(str(exc)) from exc
            self._append(result)
            self._db.execute(
                "INSERT INTO investigation_review_decisions VALUES (?,?,?,?,?)",
                (
                    command.target.matter_id,
                    command.decision_id,
                    command.target.model_dump_json(),
                    result.revision,
                    command.model_dump_json(),
                ),
            )
        return result

    def review_history(self, target: ReviewTarget) -> tuple[ReviewRevision, ...]:
        """Matter-scoped audit read, including obsolete or deleted source versions."""
        with self._lock:
            rows = self._db.execute(
                "SELECT payload FROM investigation_review_revisions "
                "WHERE matter_id=? AND target=? ORDER BY revision",
                (target.matter_id, target.model_dump_json()),
            ).fetchall()
            return tuple(ReviewRevision.model_validate_json(row[0]) for row in rows)

    def current_review(self, target: ReviewTarget) -> ReviewRevision | None:
        """No stale source is ever returned as a current accepted decision."""
        with self._lock:
            try:
                self._db.execute("BEGIN")
                result = self._latest(target) if self._source_current(target) else None
                self._db.commit()
                return result
            except Exception:
                self._db.rollback()
                raise

    def _dependency_current(
        self, dependency: ProjectionDependency, *, accepted: bool
    ) -> bool:
        for item in dependency.inputs:
            if not self._source_current(item.target):
                return False
            revision = self._latest(item.target)
            if revision is None or revision.revision != item.revision:
                return False
            if accepted:
                original = revision.original
                if revision.review_state != "ACCEPTED":
                    return False
                if not isinstance(original, EntityMention):
                    if original.verification_state != "VALID":
                        return False
                    temporal = (
                        original.temporal
                        if isinstance(original, InvestigationEvent)
                        else original.stated_time
                    )
                    if temporal is not None and temporal.verification_state != "VALID":
                        return False
        return True

    def save_projection_dependency(self, dependency: ProjectionDependency) -> None:
        dependency = ProjectionDependency.model_validate(
            dependency.model_dump(), strict=True
        )
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                if not self._dependency_current(dependency, accepted=False):
                    raise MatterError("Projection inputs are missing or stale.")
                row = self._db.execute(
                    "SELECT payload FROM investigation_projection_dependencies "
                    "WHERE matter_id=? AND projection_id=?",
                    (dependency.matter_id, dependency.projection_id),
                ).fetchone()
                if row:
                    if ProjectionDependency.model_validate_json(row[0]) != dependency:
                        raise MatterError("Projection identity conflicts.")
                else:
                    self._db.execute(
                        "INSERT INTO investigation_projection_dependencies VALUES "
                        "(?,?,?)",
                        (
                            dependency.matter_id,
                            dependency.projection_id,
                            dependency.model_dump_json(),
                        ),
                    )
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise

    def projection_is_current(
        self, matter_id: str, projection_id: str, *, require_accepted: bool = False
    ) -> bool:
        """Derived stale state is evaluated atomically, never cached as approval."""
        with self._lock:
            try:
                self._db.execute("BEGIN")
                row = self._db.execute(
                    "SELECT payload FROM investigation_projection_dependencies "
                    "WHERE matter_id=? AND projection_id=?",
                    (matter_id, projection_id),
                ).fetchone()
                current = row is not None and self._dependency_current(
                    ProjectionDependency.model_validate_json(row[0]),
                    accepted=require_accepted,
                )
                self._db.commit()
                return current
            except Exception:
                self._db.rollback()
                raise

    def load_reviewed_chronology(
        self, matter_id: str, projection_id: str, *, require_accepted: bool = False
    ) -> ReviewedChronology | None:
        """Reconstruct a persisted chronology from exact immutable input revisions.

        Check currency and load inputs within one database snapshot. A caller must
        recheck currency at a future publication boundary; this is no approval token.
        """
        with self._lock:
            try:
                self._db.execute("BEGIN")
                row = self._db.execute(
                    "SELECT payload FROM investigation_projection_dependencies "
                    "WHERE matter_id=? AND projection_id=?",
                    (matter_id, projection_id),
                ).fetchone()
                result = None
                if row:
                    dependency = ProjectionDependency.model_validate_json(row[0])
                    if self._dependency_current(dependency, accepted=require_accepted):
                        revisions: list[ReviewRevision] = []
                        for item in dependency.inputs:
                            revision = self._latest(item.target)
                            assert revision is not None
                            revisions.append(revision)
                        result = build_reviewed_chronology(
                            tuple(revisions),
                            matter_id=matter_id,
                            require_accepted=require_accepted,
                        )
                self._db.commit()
                return result
            except Exception:
                self._db.rollback()
                raise

"""Independent, deterministic verification of in-memory extraction candidates.

VALID means exact source provenance, never entailment or factual truth. Callers
supply matter-scoped trusted manifests/sections and the expected extraction run.
No review decision or calendar interpretation is inferred here.
"""

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from typing import Literal

from pydantic import ValidationError

from lextrace.investigations.contracts import (
    AttributedAssertion,
    DocumentVersion,
    ExtractionCoverage,
    ExtractionSpan,
    InvestigationEvent,
    ReviewedRecord,
    TemporalReference,
)
from lextrace.investigations.factual_extraction import (
    Candidate,
    ExtractionResult,
    QuarantinedCandidate,
)
from lextrace.investigations.provenance import validate_evidence_reference
from lextrace.investigations.source_attribution import attribution_for, source_accounts
from lextrace.matter.contracts import DocumentSection


@dataclass(frozen=True)
class CandidateDiagnostic:
    candidate_index: int
    issue_codes: tuple[str, ...]
    requires_human_review: bool


@dataclass(frozen=True)
class VerificationQuarantine:
    candidate_index: int
    candidate: Candidate
    issue_codes: tuple[str, ...]


@dataclass(frozen=True)
class CoverageVerification:
    status: Literal["COMPLETE", "PARTIAL", "OCR_REQUIRED"]
    original: ExtractionCoverage
    uncovered_spans: tuple[ExtractionSpan, ...]
    issue_codes: tuple[str, ...]


@dataclass(frozen=True)
class ExtractionVerificationResult:
    valid_candidates: tuple[Candidate, ...]
    quarantined_candidates: tuple[VerificationQuarantine, ...]
    extraction_quarantine: tuple[QuarantinedCandidate, ...]
    coverage: CoverageVerification
    diagnostics: tuple[CandidateDiagnostic, ...]


def _unique(codes: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(codes))


def _ownership(
    record: Candidate | ExtractionCoverage, version: DocumentVersion, run_id: str
) -> list[str]:
    return [
        code
        for field, code, expected in (
            ("matter_id", "MATTER_MISMATCH", version.matter_id),
            ("document_id", "DOCUMENT_MISMATCH", version.document_id),
            ("version_id", "VERSION_MISMATCH", version.version_id),
            ("run_id", "RUN_MISMATCH", run_id),
        )
        if getattr(record, field) != expected
    ]


def _coverage(
    coverage: ExtractionCoverage, version: DocumentVersion, run_id: str
) -> CoverageVerification:
    issues = _ownership(coverage, version, run_id)
    try:
        ExtractionCoverage.model_validate(coverage.model_dump(), strict=True)
    except ValidationError:
        issues.append("MALFORMED_COVERAGE")
    if coverage.text_length != version.text_length:
        issues.append("TEXT_LENGTH_MISMATCH")
    cursor = 0
    gaps: list[ExtractionSpan] = []
    for span in sorted(coverage.processed_windows, key=lambda s: (s.start, s.end)):
        if not 0 <= span.start < span.end <= version.text_length:
            issues.append("COVERAGE_BOUNDS_INVALID")
            continue
        if span.start < cursor:
            issues.append("COVERAGE_OVERLAP")
        if span.start > cursor:
            gaps.append(ExtractionSpan(start=cursor, end=span.start))
        cursor = max(cursor, span.end)
    if cursor < version.text_length:
        gaps.append(ExtractionSpan(start=cursor, end=version.text_length))
    if gaps:
        issues.append("UNCOVERED_TEXT")
    for span in (*coverage.processed_windows, *coverage.skipped_spans):
        if not 0 <= span.start < span.end <= version.text_length:
            issues.append("COVERAGE_BOUNDS_INVALID")
        if span.page_start is not None and (
            not version.page_starts
            or span.page_start != sum(s <= span.start for s in version.page_starts)
            or span.page_end != sum(s < span.end for s in version.page_starts)
        ):
            issues.append("PAGE_MISMATCH")
    all_spans = sorted(
        (*coverage.processed_windows, *coverage.skipped_spans),
        key=lambda span: (span.start, span.end),
    )
    furthest = 0
    for span in all_spans:
        if span.start < furthest:
            issues.append("COVERAGE_OVERLAP")
        furthest = max(furthest, span.end)
    pages = (*coverage.processed_pages, *coverage.excluded_pages)
    if any(not 1 <= page <= len(version.page_starts) for page in pages):
        issues.append("PAGE_MISMATCH")
    if coverage.skipped_spans:
        issues.append("UNPROCESSED_WINDOWS")
    if coverage.excluded_pages:
        issues.append("EXCLUDED_PAGES")
    if coverage.incomplete_reasons:
        issues.append("EXTRACTION_INCOMPLETE")
    if version.coverage != "COMPLETE":
        issues.append("SOURCE_" + version.coverage)
    if not version.text_length:
        issues.append("EMPTY_EXTRACTED_TEXT")
    status: Literal["COMPLETE", "PARTIAL", "OCR_REQUIRED"] = "COMPLETE"
    if version.coverage == "OCR_REQUIRED" or coverage.status == "OCR_REQUIRED":
        status = "OCR_REQUIRED"
    elif issues or coverage.status == "PARTIAL":
        status = "PARTIAL"
    if coverage.status == "COMPLETE" and status != "COMPLETE":
        issues.append("MISLEADING_COMPLETE")
    return CoverageVerification(status, coverage, tuple(gaps), _unique(issues))


def verify_extraction(
    result: ExtractionResult,
    version: DocumentVersion,
    sections: tuple[DocumentSection, ...],
    text: str,
    *,
    run_id: str,
) -> ExtractionVerificationResult:
    """Recheck every reference, including nested time, without trusting caches.

    Invalid trusted inputs raise ValueError. Invalid candidates are isolated.
    Bounded dates are supported only for verbatim ISO calendar days; other calendar
    interpretations are quarantined for review rather than silently normalized.
    Unbounded temporal expressions and source accounts remain human-review items.
    Returned records retain review metadata and receive fresh provenance states.
    """
    version = DocumentVersion.model_validate(version.model_dump(), strict=True)
    if not run_id.strip():
        raise ValueError("Expected run identity is required.")
    try:
        digest = sha256(text.encode("utf-8")).hexdigest()
    except UnicodeEncodeError as exc:
        raise ValueError("INVALID_UNICODE") from exc
    if len(text) != version.text_length or digest != version.extracted_text_hash:
        raise ValueError("Source text differs from trusted version.")
    section_map: dict[str, DocumentSection] = {}
    for section in sections:
        section = DocumentSection.model_validate(section.model_dump(), strict=True)
        if section.section_id in section_map:
            raise ValueError("Duplicate trusted section identity.")
        section_map[section.section_id] = section

    accounts = source_accounts(version, sections, text)

    def check(record: Candidate) -> tuple[Candidate, tuple[str, ...]]:
        issues = _ownership(record, version, run_id)
        if version.coverage == "OCR_REQUIRED":
            issues.append("SOURCE_OCR_REQUIRED")
        try:
            type(record).model_validate(record.model_dump(), strict=True)
        except ValidationError:
            issues.append("MALFORMED_CANDIDATE")
        for ref in record.evidence_references:
            section = section_map.get(getattr(ref, "section_id", ""))
            if section is None:
                issues.append("SECTION_MISMATCH")
                # Still revalidate every reference, even when resolution failed.
                section = DocumentSection.model_construct(
                    section_id="missing",
                    document_id=version.document_id,
                    order=0,
                    kind="paragraph",
                    text="",
                    span=None,
                )
            issues.extend(validate_evidence_reference(ref, version, section, text))
        if isinstance(record, AttributedAssertion) and (
            record.generator == "investigation-factual-extraction@3.0.0"
            or record.attribution_reference is not None
        ):
            resolved = [
                attribution_for(accounts, ref.start, ref.end)
                for ref in record.evidence_references
            ]
            if record.attributed_to is not None and (
                any(
                    a is None
                    or a.speaker != record.attributed_to
                    or a.attribution != record.attribution_reference
                    for a in resolved
                )
            ):
                issues.append("ATTRIBUTION_STRUCTURE_MISMATCH")
            if record.attribution_reference is not None:
                ref = record.attribution_reference
                section = section_map.get(ref.section_id)
                if section is None:
                    issues.append("ATTRIBUTION_SECTION_MISSING")
                else:
                    issues.extend(
                        validate_evidence_reference(ref, version, section, text)
                    )
        if isinstance(record, TemporalReference):
            raw = record.raw_expression
            if raw is not None and not all(
                raw in getattr(ref, "exact_quote", "")
                for ref in record.evidence_references
            ):
                issues.append("TEMPORAL_EXPRESSION_UNSOURCED")
            if record.earliest is not None:
                supported = False
                if record.kind == "exact" and raw is not None:
                    try:
                        parsed = date.fromisoformat(raw)
                        supported = (
                            raw == parsed.isoformat()
                            and parsed == record.earliest == record.latest
                        )
                    except ValueError:
                        pass
                if not supported:
                    issues.append("TEMPORAL_BOUNDS_UNSUPPORTED")
        if isinstance(record, ReviewedRecord) and (
            record.review_state != "PROPOSED"
            or record.review_decision_id is not None
            or record.interpretation_basis == "REVIEWED_FINDING"
        ):
            issues.append("UNEXPECTED_REVIEW_METADATA")
        updates: dict[str, object] = {}
        nested = (
            record.temporal
            if isinstance(record, InvestigationEvent)
            else record.stated_time
            if isinstance(record, AttributedAssertion)
            else None
        )
        if nested is not None:
            verified, nested_issues = check(nested)
            issues.extend(nested_issues)
            updates[
                "temporal" if isinstance(record, InvestigationEvent) else "stated_time"
            ] = verified
        if isinstance(record, ReviewedRecord):
            updates["verification_state"] = "INVALID" if issues else "VALID"
        return record.model_copy(update=updates), _unique(issues)

    valid: list[Candidate] = []
    quarantine: list[VerificationQuarantine] = []
    diagnostics: list[CandidateDiagnostic] = []
    for index, candidate in enumerate(result.candidates):
        verified, issues = check(candidate)
        if issues:
            quarantine.append(VerificationQuarantine(index, verified, issues))
        else:
            valid.append(verified)
        needs_review = isinstance(candidate, ReviewedRecord)
        diagnostics.append(
            CandidateDiagnostic(
                index,
                (*issues, "SEMANTIC_REVIEW_REQUIRED") if needs_review else issues,
                needs_review,
            )
        )
    logging.getLogger(__name__).info(
        "factual_verification_summary",
        extra={
            "verification_counts": {
                "accepted_by_kind": dict(Counter(type(c).__name__ for c in valid)),
                "rejected_by_reason": dict(
                    Counter(code for q in quarantine for code in q.issue_codes)
                ),
                "extraction_quarantined": len(result.quarantined),
                "verified_events": sum(
                    isinstance(c, InvestigationEvent) for c in valid
                ),
            }
        },
    )
    return ExtractionVerificationResult(
        tuple(valid),
        tuple(quarantine),
        result.quarantined,
        _coverage(result.coverage, version, run_id),
        tuple(diagnostics),
    )

"""Predictable editable reports, with no inference or provider calls."""

from collections import Counter
from dataclasses import dataclass

from lextrace.deposition.contracts import PlanView
from lextrace.deposition.service import PublishedContext
from lextrace.exports.document import (
    Download,
    ExportError,
    Sources,
    WordReport,
    attachment_name,
)
from lextrace.investigations.contracts import (
    AttributedAssertion,
    EntityMention,
    InvestigationEvent,
)
from lextrace.investigations.findings import Finding
from lextrace.investigations.review import ReviewRevision, target_of
from lextrace.matter.contracts import Matter

DISCLAIMER = (
    "Legal research and preparation information only. This document is not legal "
    "advice and does not establish factual truth, credibility, dishonesty, "
    "liability or an outcome. Review approval records the stated human decision; "
    "it does not certify an allegation. Later Word edits are outside the "
    "recorded LexTrace approval."
)


@dataclass(frozen=True)
class ReportInput:
    matter: Matter
    context: PublishedContext
    plan: PlanView | None = None
    current_plan_revision: int | None = None


def add_record_sources(sources: Sources, record: ReviewRevision) -> None:
    for ref in record.original.evidence_references:
        sources.add(ref, record.original.run_id)
    temporal = getattr(
        record.original, "stated_time", getattr(record.original, "temporal", None)
    )
    if temporal:
        for ref in temporal.evidence_references:
            sources.add(ref, record.original.run_id)


def report_sources(data: ReportInput) -> Sources:
    sources = Sources()
    if data.plan is None:
        for revision in data.context.revisions:
            add_record_sources(sources, revision)
        result = data.context.artifacts.result
        if result is not None:
            for finding_ in result.findings:
                for account in finding_.accounts:
                    sources.add(account.reference)
    else:
        registry = {
            item.evidence_id: item.reference for item in data.plan.basis.evidence
        }
        selected = {q.witness_id for q in data.plan.current.questions}
        for witness in data.plan.basis.witnesses:
            if witness.witness_id not in selected:
                continue
            for revision in (*witness.accounts, *witness.chronology):
                add_record_sources(sources, revision)
            for finding_ in witness.findings:
                for account in finding_.accounts:
                    sources.add(account.reference)
        for question in data.plan.current.questions:
            for identity in question.evidence_ids:
                if identity not in registry:
                    raise ExportError("EXPORT_SOURCE_CONFLICT")
                sources.add(registry[identity])
    sources.freeze()
    return sources


def introduction(report: WordReport, data: ReportInput) -> None:
    signed = data.context.snapshot
    report.add(data.matter.name, "Subtitle")
    report.add(
        "This report organizes recorded reviewed source "
        "material for legal preparation. Source allegations "
        "and investigative hypotheses remain subject to "
        "verification and human judgment."
    )
    report.metadata("Matter reference", data.matter.reference or "Not recorded")
    report.metadata("Matter ID", signed.matter_id)
    report.metadata("Investigation run", signed.run_id)
    report.metadata("Investigation thread", signed.thread_id)
    report.metadata("Published snapshot", signed.snapshot_id)
    report.metadata("Signed artifact digest", signed.artifact_digest)
    report.metadata("Source dependency digest", signed.source_dependency_digest)
    report.metadata("Review dependency digest", signed.review_dependency_digest)
    report.metadata(
        "Investigation review status", "Human signed investigative hypotheses"
    )
    report.metadata("Investigation reviewer", signed.reviewer_id)
    report.metadata("Investigation approval time", "Not recorded")
    report.add(DISCLAIMER)


def coverage(report: WordReport, data: ReportInput) -> None:
    report.heading("Coverage and limitations")
    verified = data.context.artifacts.verified
    report.add(
        "Coverage statuses: "
        + (", ".join(v.coverage.status for v in verified) or "Not recorded")
    )
    if not verified or any(v.coverage.status != "COMPLETE" for v in verified):
        report.add(
            "PARTIAL OR INCOMPLETE COVERAGE  This material must "
            "not be treated as a complete account of the source "
            "collection."
        )
    quarantined = sum(
        len(v.quarantined_candidates) + len(v.extraction_quarantine) for v in verified
    )
    report.add(
        "Quarantined candidates excluded from reviewed "
        "content: "
        f"{quarantined}"
        ". Exclusion does not imply that no other evidence "
        "exists."
    )
    for item in verified:
        original = item.coverage.original
        report.metadata("Coverage extraction run", original.run_id)
        for warning in (
            *original.warnings,
            *original.incomplete_reasons,
            *item.coverage.issue_codes,
        ):
            report.add(f"Recorded coverage limitation: {warning}")
    if data.context.artifacts.result:
        result = data.context.artifacts.result
        if result.partial:
            report.add("The recorded investigation result is partial.")
        for diagnostic in result.diagnostics:
            report.add(
                f"Investigation diagnostic: {diagnostic.code} ({diagnostic.count})."
            )
    if data.plan:
        report.metadata("Preparation scope", data.plan.basis.outcome)
        for limitation in data.plan.basis.limitations:
            report.add(f"Plan limitation: {limitation}")
    report.add(
        "No missing, rejected or quarantined material has "
        "been filled with invented testimony, dates or "
        "conclusions."
    )


def record(report: WordReport, revision: ReviewRevision, sources: Sources) -> None:
    original = revision.original
    identity = target_of(original)
    report.metadata(
        "Record",
        (
            f"{identity.kind}"
            " "
            f"{identity.record_id}"
            "; extraction run "
            f"{original.run_id}"
            "; review revision "
            f"{revision.revision}"
        ),
    )
    report.metadata("Human review state", revision.review_state)
    if revision.decision:
        report.metadata("Record reviewer", revision.decision.reviewer_id)
        report.metadata("Record review time", revision.decision.timestamp.isoformat())
    if isinstance(original, AttributedAssertion):
        report.add("Attributed allegation or source account  Not an established fact.")
        report.metadata(
            "Source attribution",
            f"{original.attributed_to or 'Unresolved'} ({original.attribution_status})",
        )
        report.add(original.normalized_proposition)
        report.metadata(
            "Modality and dispute", f"{original.modality}; {original.dispute_status}"
        )
        if original.uncertainty:
            report.add(f"Recorded uncertainty: {original.uncertainty}")
    elif isinstance(original, InvestigationEvent):
        report.add(
            "Reviewed event interpretation  Not independent proof of occurrence."
        )
        report.add(original.description)
        if original.uncertainty:
            report.add(f"Recorded uncertainty: {original.uncertainty}")
    elif isinstance(original, EntityMention):
        report.add(f"Explicit mention: {original.mention_text}")
        report.metadata(
            "Reviewed entity association", revision.resolved_entity_id or "Unresolved"
        )
        report.add(
            "An entity association is not a speaker identity "
            "merge or proof of personal knowledge."
        )
    temporal = getattr(original, "stated_time", getattr(original, "temporal", None))
    if temporal:
        report.metadata(
            "Source time expression", temporal.raw_expression or "Unresolved"
        )
        report.metadata("Source timezone", temporal.timezone or "Not recorded")
        report.metadata(
            "Source time interpretation",
            (
                f"{temporal.kind}"
                "; "
                f"{temporal.earliest}"
                " to "
                f"{temporal.latest}"
                "; precision "
                f"{temporal.precision}"
            ),
        )
        if temporal.evidence_references != original.evidence_references:
            report.add(sources.cite(temporal.evidence_references))
    if revision.date_correction:
        correction = revision.date_correction
        report.metadata(
            "Reviewer date interpretation",
            (
                f"{correction.kind}"
                "; "
                f"{correction.earliest}"
                " to "
                f"{correction.latest}"
                "; precision "
                f"{correction.precision}"
            ),
        )
    report.add(sources.cite(original.evidence_references))
    for ref in original.evidence_references:
        report.add(ref.exact_quote, "Quote")


def finding(report: WordReport, item: Finding, sources: Sources) -> None:
    report.heading("Recorded investigative hypothesis", 2)
    report.metadata("Finding ID", item.finding_id)
    report.metadata("Comparison pair", item.pair_id)
    report.metadata("Finding status", item.status)
    report.metadata("Finding review state", item.review_state)
    report.metadata("Independent assessment", item.verification)
    report.add(
        "Potential inconsistency is not a proven "
        "contradiction or evidence of dishonesty. Snapshot "
        "approval does not turn this hypothesis into a fact."
    )
    from lextrace.investigations.inconsistency_investigation import evidence_id

    allowed = {evidence_id(account.reference) for account in item.accounts}
    if not allowed or not allowed.issubset(sources.references):
        raise ExportError("EXPORT_SOURCE_CONFLICT")
    for assessment in (item.hypothesis, item.independent_assessment):
        mentioned = {key for p in assessment.premises for key in p.evidence_ids}
        mentioned.update(key for c in assessment.checks for key in c.evidence_ids)
        mentioned.update(key for a in assessment.alternatives for key in a.evidence_ids)
        mentioned.update(a.evidence_id for a in assessment.evidence)
        if not mentioned.issubset(allowed):
            raise ExportError("EXPORT_SOURCE_CONFLICT")
    for i, account in enumerate(item.accounts, 1):
        report.add(f"Recorded source passage {i}. {sources.cite((account.reference,))}")
        report.add(account.reference.exact_quote, "Quote")
    assessments = (
        (("Initial and independent assessments agree", item.hypothesis),)
        if item.hypothesis == item.independent_assessment
        else (
            ("Initial assessment", item.hypothesis),
            ("Independent assessment", item.independent_assessment),
        )
    )
    for label, assessment in assessments:
        report.add(
            f"{label}"
            ": "
            f"{assessment.status}"
            ". Interpretations remain unverified hypotheses."
        )
        for side in ("left", "right"):
            premises = [p for p in assessment.premises if p.side == side]
            if premises:
                report.metadata(
                    f"{side.title()} premise assessments",
                    "; ".join(f"{p.dimension} {p.entailment}" for p in premises),
                )
                report.add(
                    sources.cite_ids(
                        tuple(sorted({key for p in premises for key in p.evidence_ids}))
                    )
                )
        report.metadata(
            "Comparability assessments",
            "; ".join(f"{c.dimension} {c.assessment}" for c in assessment.checks),
        )
        for alternative in assessment.alternatives:
            report.metadata(
                "Alternative explanation assessment",
                f"{alternative.explanation.replace('_', ' ')}: "
                f"{alternative.assessment.replace('_', ' ')}; "
                f"{sources.cite_ids(alternative.evidence_ids)}",
            )
        for evidence in assessment.evidence:
            report.metadata(
                "Source role assessment",
                f"{sources.cite_ids((evidence.evidence_id,))}: "
                f"{evidence.role}; entailment {evidence.entailment}",
            )


def investigation(data: ReportInput) -> Download:
    sources = report_sources(data)
    report = WordReport("Investigation Report")
    introduction(report, data)
    report.heading("Executive summary")
    counts = Counter(target_of(r.original).kind for r in data.context.revisions)
    findings = (
        data.context.artifacts.result.findings if data.context.artifacts.result else ()
    )
    report.add(
        f"The signed snapshot records {counts['assertion']} reviewed attributed "
        f"accounts, {counts['event']} reviewed events, {counts['mention']} "
        f"reviewed entity mentions and {len(findings)} investigative findings. "
        "This is a record count summary, not a new conclusion."
    )
    coverage(report, data)
    report.heading("Reviewed chronology")
    chronology = data.context.artifacts.reviewed_chronology
    if chronology is None:
        raise ExportError("EXPORT_SOURCE_CONFLICT")
    if not chronology.dated_groups and not chronology.unresolved_events:
        report.add(
            "No reviewed chronology events were recorded. No sequence is inferred."
        )
    else:
        report.add(
            "Inclusive overlapping date ranges form groups. Order within a group "
            "is unresolved; overlapping dates do not establish sequence."
        )
    for group in chronology.dated_groups:
        report.heading(f"Date range {group.earliest} to {group.latest}", 2)
        for event in group.events:
            record(report, event, sources)
    if chronology.unresolved_events:
        report.heading("Events with unresolved dates", 2)
        for event in chronology.unresolved_events:
            record(report, event, sources)
    if chronology.excluded_events:
        raise ExportError("EXPORT_SOURCE_CONFLICT")
    report.heading("Reviewed evidence accounts")
    accounts = [
        r for r in data.context.revisions if isinstance(r.original, AttributedAssertion)
    ]
    if not accounts:
        report.add("No reviewed attributed accounts were recorded.")
    for i, account in enumerate(accounts, 1):
        report.heading(f"Source account {i}", 2)
        record(report, account, sources)
    report.heading("Reviewed identities and participant associations")
    mentions = [
        r for r in data.context.revisions if isinstance(r.original, EntityMention)
    ]
    if not mentions:
        report.add("No reviewed entity mentions were recorded.")
    for mention in mentions:
        record(report, mention, sources)
    for link in data.context.artifacts.entity_links:
        report.metadata(
            "Account participant association",
            f"{link.account.record_id} linked to mentions "
            + ", ".join(m.record_id for m in link.mentions),
        )
    report.heading("Findings and potential inconsistencies")
    if not findings:
        report.add(
            "No reviewed investigative findings were recorded. "
            "Absence of a finding does not establish consistency "
            "or completeness."
        )
    for item in findings:
        finding(report, item, sources)
    report.appendix(sources)
    return Download(
        report.finish(),
        attachment_name(
            data.matter.name, "investigation", data.context.snapshot.snapshot_id
        ),
    )


def deposition(data: ReportInput) -> Download:
    view = data.plan
    if view is None or view.current.status != "APPROVED" or not view.current.questions:
        raise ExportError("EXPORT_APPROVAL_REQUIRED")
    sources = report_sources(data)
    report = WordReport("Deposition Preparation")
    introduction(report, data)
    report.heading("Approved preparation metadata")
    for label, value in (
        ("Plan ID", view.basis.plan_id),
        ("Approved revision", view.current.revision),
        ("Plan reviewer", view.current.reviewer_id),
        ("Plan approval time", view.current.timestamp.isoformat()),
        ("Plan decision ID", view.current.decision_id),
        ("Approval status", view.current.status),
    ):
        report.metadata(label, value)
    if data.current_plan_revision != view.current.revision:
        report.add(
            "Historical approved revision. The current plan "
            "revision is "
            f"{data.current_plan_revision}"
            "; later drafts or decisions are not included in this"
            " export."
        )
    report.add(
        "Only included questions from the exact approved "
        "revision follow. Rejected and deferred questions and"
        " mutable draft wording are excluded. Suggested "
        "questions are not established testimony."
    )
    coverage(report, data)
    by_witness = {w.witness_id: w for w in view.basis.witnesses}
    seen: set[str] = set()
    related_findings: dict[str, Finding] = {}
    previous_witness: str | None = None
    ordered = sorted(view.current.questions, key=lambda q: (q.order, q.question_id))
    for number, question in enumerate(ordered, 1):
        if question.disposition != "INCLUDE" or question.witness_id not in by_witness:
            raise ExportError("EXPORT_SOURCE_CONFLICT")
        witness = by_witness[question.witness_id]
        if witness.witness_id not in seen:
            report.heading("Witness account dossier")
            report.add(witness.label, "Heading 2")
            report.metadata("Account scoped witness ID", witness.witness_id)
            report.add(
                "Equal or similar names have not been merged. "
                "Participant links do not establish speaker identity "
                "or personal knowledge."
            )
            for uncertainty in witness.uncertainties:
                report.add(f"Recorded uncertainty: {uncertainty}")
            for account in witness.accounts:
                record(report, account, sources)
            report.heading("Account timing and participant context", 2)
            if not witness.chronology:
                report.add(
                    "No reviewed timing or related participant chronology"
                    " was recorded for this account."
                )
            related_events = [
                event for event in witness.chronology if event not in witness.accounts
            ]
            if witness.chronology and not related_events:
                report.add(
                    "Account timing is recorded with the source account "
                    "above. No additional event sequence is inferred."
                )
            for event in related_events:
                record(report, event, sources)
            for item in witness.findings:
                related_findings[item.finding_id] = item
                report.metadata("Related hypothesis", item.finding_id)
            seen.add(witness.witness_id)
        elif previous_witness != witness.witness_id:
            report.heading("Continuing witness account")
            report.add(witness.label, "Heading 2")
        previous_witness = witness.witness_id
        # Global approved order is retained, even when witnesses/categories recur.
        report.heading(
            f"Question {number} {question.category.replace('_', ' ').title()}",
            2,
        )
        # Keep a question heading, its identifiers, wording and citations together
        # when they fit. Long quotes may still flow naturally across pages.
        identifiers = [
            ("Witness account", f"{witness.label}; {witness.witness_id}"),
            ("Approved order value", str(question.order)),
            ("Question ID", question.question_id),
        ]
        if question.group:
            identifiers.append(("Reviewer preparation group", question.group))
        for label, value in identifiers:
            report.metadata(label, value).paragraph_format.keep_with_next = True
        report.add(question.wording).paragraph_format.keep_with_next = True
        report.add(
            f"Reviewer approved rationale: {question.rationale}"
        ).paragraph_format.keep_with_next = True
        report.add(sources.cite_ids(question.evidence_ids))
        for index, key in enumerate(question.evidence_ids):
            if key not in sources.references:
                raise ExportError("EXPORT_SOURCE_CONFLICT")
            quote = report.add(sources.references[key].exact_quote, "Quote")
            quote.paragraph_format.keep_with_next = index + 1 < len(
                question.evidence_ids
            )
    report.heading("Relevant investigative hypotheses and follow up context")
    if not related_findings:
        report.add(
            "No related independently supported potential "
            "inconsistencies were recorded for these witness "
            "accounts."
        )
    for identity in sorted(related_findings):
        finding(report, related_findings[identity], sources)
    report.appendix(sources)
    return Download(
        report.finish(),
        attachment_name(
            data.matter.name, "deposition", view.basis.plan_id, view.current.revision
        ),
    )

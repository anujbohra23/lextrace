"""Evidence Matrix v2 projections and safe CSV serialization."""

import csv
import io

from lextrace.graph.courts import classify_authority
from lextrace.matter.research_contracts import EvidenceMatrixRow
from lextrace.matter.store import MatterStore


def matrix_rows(store: MatterStore, matter_id: str) -> list[EvidenceMatrixRow]:
    matter = store.get_matter(matter_id)
    if matter is None:
        from lextrace.matter.contracts import MatterError

        raise MatterError("Matter was not found.")
    issues = {issue.issue_id: issue.label for issue in store.all_issues(matter_id)}
    documents = {
        doc.document_id: doc.filename for doc in store.list_documents(matter_id)
    }
    rows: list[EvidenceMatrixRow] = []
    for claim in store.all_claims(matter_id):
        finding = store.finding(matter_id, claim.claim_id)
        coverage = store.research_coverage(claim.claim_id)
        doctrine = store.latest_doctrine(matter_id, claim.claim_id)
        attacks = [a for a in store.attacks(matter_id, claim.claim_id) if a.verified]
        matter_facts = [
            fact
            for fact in store.facts(matter_id, claim.document_id)
            if fact.span.start < claim.span.end and fact.span.end > claim.span.start
        ]
        cited = [
            item
            for item in (finding.evidence if finding else [])
            if item.role == "cited"
        ]
        support = next(
            (
                item.result.case_id
                for item in (finding.evidence if finding else [])
                if item.role == "independent_support"
            ),
            None,
        )
        counter = next(
            (
                item.evidence.result.case_id
                for item in (finding.counter_authorities if finding else [])
            ),
            None,
        )
        authority = "UNKNOWN"
        if cited:
            authority = classify_authority(
                cited[0].result.case_id, cited[0].result.court, matter.court
            ).category
        severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, None: 4}
        severity = (
            min((a.severity for a in attacks), key=lambda value: severity_order[value])
            if attacks
            else None
        )
        rows.append(
            EvidenceMatrixRow(
                claim_id=claim.claim_id,
                issue_id=claim.issue_id,
                issue=issues.get(claim.issue_id or "", "Unassigned"),
                claim=claim.normalized_proposition,
                document_id=claim.document_id,
                document_name=documents.get(claim.document_id, "Document"),
                source_start=claim.span.start,
                source_end=claim.span.end,
                matter_evidence_ids=[fact.fact_id for fact in matter_facts],
                cited_case_ids=[item.result.case_id for item in cited],
                cited_source_urls=[str(item.result.source_url) for item in cited],
                citation_support=finding.verification_status if finding else "UNJUDGED",
                authority_category=authority,
                strongest_support_case_id=support,
                strongest_counter_case_id=counter,
                later_treatment=[event.category for event in doctrine.events]
                if doctrine
                else [],
                doctrine_state=doctrine.impact.category if doctrine else "NOT_REVIEWED",
                coverage=coverage.category if coverage else "UNKNOWN",
                gaps=[gap.type for gap in coverage.gaps if not gap.resolved]
                if coverage
                else [],
                attack_severity=severity,
                attack_ids=[a.attack_id for a in attacks],
                argument_status=finding.vulnerability if finding else "NOT_ANALYZED",
                importance=claim.importance,
            )
        )
    return rows


def filter_matrix(
    rows: list[EvidenceMatrixRow],
    *,
    issue: str | None = None,
    status: str | None = None,
    authority: str | None = None,
    citation_support: str | None = None,
    coverage: str | None = None,
    severity: str | None = None,
    unresolved_only: bool = False,
    document: str | None = None,
    sort: str = "vulnerability",
) -> list[EvidenceMatrixRow]:
    selected = [
        row
        for row in rows
        if (not issue or row.issue_id == issue)
        and (not status or row.argument_status == status)
        and (not authority or row.authority_category == authority)
        and (not citation_support or row.citation_support == citation_support)
        and (not coverage or row.coverage == coverage)
        and (not severity or row.attack_severity == severity)
        and (not unresolved_only or bool(row.gaps))
        and (not document or row.document_id == document)
    ]
    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, None: 4}
    coverage_order = {
        "INSUFFICIENT": 0,
        "WEAK": 1,
        "UNKNOWN": 2,
        "PARTIAL": 3,
        "SUFFICIENT": 4,
    }
    if sort == "coverage":
        return sorted(
            selected, key=lambda row: (coverage_order[row.coverage], row.claim_id)
        )
    if sort == "issue":
        return sorted(selected, key=lambda row: (row.issue, row.claim_id))
    if sort == "authority":
        return sorted(selected, key=lambda row: (row.authority_category, row.claim_id))
    if sort == "importance":
        importance = {"high": 0, "medium": 1, "low": 2}
        return sorted(
            selected, key=lambda row: (importance[row.importance], row.claim_id)
        )
    return sorted(selected, key=lambda row: (order[row.attack_severity], row.claim_id))


def matrix_csv(rows: list[EvidenceMatrixRow], matter_name: str) -> str:
    """Export references only; guard spreadsheet formula injection."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "matter",
            "issue",
            "claim",
            "status",
            "cited_case_ids",
            "authority_category",
            "counter_case_id",
            "coverage",
            "attack_severity",
            "research_gaps",
            "source_urls",
        ]
    )

    def safe(value: str) -> str:
        return "'" + value if value.startswith(("=", "+", "-", "@")) else value

    for row in rows:
        writer.writerow(
            [
                safe(matter_name),
                safe(row.issue),
                safe(row.claim),
                row.argument_status,
                ";".join(row.cited_case_ids),
                row.authority_category,
                row.strongest_counter_case_id or "",
                row.coverage,
                row.attack_severity or "",
                ";".join(row.gaps),
                ";".join(row.cited_source_urls),
            ]
        )
    return output.getvalue()

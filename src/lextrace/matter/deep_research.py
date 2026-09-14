"""Bounded, review-gated research over the existing retrieval contract."""

import hashlib
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from lextrace.graph.courts import classify_authority, court_for
from lextrace.matter.contracts import ArgumentFinding, LegalClaim, Matter
from lextrace.matter.research_contracts import (
    DeepResearchPlan,
    DeepResearchRun,
    ResearchCoverage,
    ResearchDiscovery,
    ResearchGap,
    ResearchRound,
    ResearchStep,
    StopReason,
)
from lextrace.retrieval.contracts import SearchRequest, SearchResponse


class Searcher(Protocol):
    def search_response(self, request: SearchRequest) -> SearchResponse: ...


def claim_fingerprint(claim: LegalClaim) -> str:
    relevant = (
        claim.normalized_proposition,
        claim.document_id,
        claim.issue_id or "",
        str(claim.span.start),
        str(claim.span.end),
        str(claim.irrelevant),
    )
    return hashlib.sha256("\0".join(relevant).encode()).hexdigest()


def assess_coverage(
    matter: Matter,
    claim: LegalClaim,
    finding: ArgumentFinding,
    discoveries: list[ResearchDiscovery] | None = None,
    *,
    queries_executed: int = 0,
) -> ResearchCoverage:
    """Assess recorded signals without claiming corpus completeness."""
    found = list(discoveries or [])
    results = [
        e.result
        for e in finding.evidence
        if not any(
            link.removed and link.evidence_id == e.evidence_id
            for link in finding.cited_authorities
        )
    ] + [d.result for d in found]
    unique = {result.case_id: result for result in results}
    relationships = {
        case_id: classify_authority(case_id, result.court, matter.court).category
        for case_id, result in unique.items()
    }
    support = {
        link.case_id
        for link in finding.cited_authorities
        if not link.removed and link.relation in {"SUPPORTS", "PARTIALLY_SUPPORTS"}
    }
    counter = {c.evidence.result.case_id for c in finding.counter_authorities}
    counter.update(
        link.case_id
        for link in finding.cited_authorities
        if not link.removed and link.relation in {"CONTRADICTS", "COUNTERS"}
    )
    # Search hits are unjudged until the existing claim verifier confirms them.
    gaps: list[ResearchGap] = []

    def add(code: str, explanation: str, objective: str, priority: str) -> None:
        gaps.append(
            ResearchGap.model_validate(
                {
                    "gap_id": hashlib.sha256(
                        f"{claim.claim_id}:{code}".encode()
                    ).hexdigest()[:16],
                    "claim_id": claim.claim_id,
                    "type": code,
                    "explanation": explanation,
                    "objective": objective,
                    "priority": priority,
                }
            )
        )

    forum = court_for(matter.court)
    controlling = sum(
        value == "CONTROLLING"
        or (
            value == "SAME_COURT"
            and forum is not None
            and forum.level in {"appeals", "supreme"}
        )
        for value in relationships.values()
    )
    if controlling == 0:
        add(
            "NO_CONTROLLING_AUTHORITY",
            "No controlling institutional authority is recorded.",
            "Search the forum's controlling court and the Supreme Court.",
            "HIGH",
        )
    if not counter:
        add(
            "NO_COUNTER_AUTHORITY",
            "No verified contrary authority is recorded.",
            "Search for decisions limiting or rejecting the proposition.",
            "MEDIUM",
        )
    if finding.unresolved_citation_ids:
        add(
            "UNRESOLVED_CITATION",
            "One or more source citations remain unresolved.",
            "Resolve each source citation to a case and exact passage.",
            "HIGH",
        )
    if not support:
        add(
            "INSUFFICIENT_EVIDENCE",
            "No supporting case passage is recorded.",
            "Find a case passage supporting this proposition.",
            "HIGH",
        )
    if finding.verification_status in {"UNSUPPORTED", "INSUFFICIENT_EVIDENCE"}:
        add(
            "MISSING_ELEMENT_RESEARCH",
            "Claim support is not verified.",
            "Research the elements and limits of the claimed rule.",
            "HIGH",
        )
    if not unique or finding.research_coverage == "NOT_SEARCHED":
        category = "UNKNOWN"
    elif not support or finding.verification_status == "UNSUPPORTED":
        category = "INSUFFICIENT"
    elif (
        controlling
        and counter
        and not finding.unresolved_citation_ids
        and queries_executed
    ):
        category = "SUFFICIENT"
    elif controlling and support:
        category = "PARTIAL"
    else:
        category = "WEAK"
    return ResearchCoverage.model_validate(
        {
            "claim_id": claim.claim_id,
            "issue_id": claim.issue_id,
            "category": category,
            "queries_executed": queries_executed,
            "authorities_retrieved": len(unique),
            "controlling_authorities": controlling,
            "persuasive_authorities": sum(
                v == "PERSUASIVE" for v in relationships.values()
            ),
            "supporting_authorities": len(support),
            "contrary_authorities": len(counter),
            "unresolved_citations": len(finding.unresolved_citation_ids),
            "gaps": gaps,
        }
    )


def propose_plan(
    matter: Matter, claim: LegalClaim, coverage: ResearchCoverage
) -> DeepResearchPlan:
    intents = {
        "NO_CONTROLLING_AUTHORITY": "CONTROLLING_AUTHORITY",
        "NO_COUNTER_AUTHORITY": "COUNTER_AUTHORITY",
        "UNRESOLVED_CITATION": "DIRECT_SUPPORT",
        "INSUFFICIENT_EVIDENCE": "DIRECT_SUPPORT",
        "MISSING_ELEMENT_RESEARCH": "ELEMENT_SPECIFIC",
    }
    steps: list[ResearchStep] = []
    for gap in coverage.gaps[:6]:
        query = claim.normalized_proposition
        if gap.type == "NO_COUNTER_AUTHORITY":
            query = f"limitations exceptions to {query}"
        elif gap.type == "NO_CONTROLLING_AUTHORITY" and matter.court:
            query = f"{matter.court} {query}"
        steps.append(
            ResearchStep.model_validate(
                {
                    "step_id": uuid.uuid4().hex,
                    "claim_id": claim.claim_id,
                    "gap_id": gap.gap_id,
                    "intent": intents.get(gap.type, "DIRECT_SUPPORT"),
                    "query": query[:4000],
                    "objective": gap.objective,
                    "desired_authority_relationship": (
                        "CONTROLLING"
                        if gap.type == "NO_CONTROLLING_AUTHORITY"
                        else None
                    ),
                    "priority": gap.priority,
                }
            )
        )
    return DeepResearchPlan(
        plan_id=uuid.uuid4().hex,
        matter_id=matter.matter_id,
        claim_id=claim.claim_id,
        claim_fingerprint=claim_fingerprint(claim),
        steps=steps,
    )


def execute_plan(
    searcher: Searcher,
    matter: Matter,
    claim: LegalClaim,
    finding: ArgumentFinding,
    plan: DeepResearchPlan,
    run: DeepResearchRun,
    *,
    stopped: Callable[[], bool] | None = None,
    progress: Callable[[DeepResearchRun], None] | None = None,
) -> DeepResearchRun:
    """Search only approved steps, with strict per-run and wall-clock bounds."""
    if plan.status != "APPROVED" or plan.claim_fingerprint != claim_fingerprint(claim):
        raise ValueError("Research plan is not approved for the current claim.")
    started = time.monotonic()
    seen_cases = {e.result.case_id for e in finding.evidence}
    seen_passages = {e.result.relevant_passage.passage_id for e in finding.evidence}
    used_steps: set[str] = set()
    discoveries: list[ResearchDiscovery] = []
    rounds: list[ResearchRound] = []
    queries = 0
    stop_reason: StopReason = "LIMIT_REACHED"
    for round_number in range(1, plan.max_rounds + 1):
        new_cases: list[str] = []
        new_passages: list[str] = []
        duplicate_cases: list[str] = []
        round_queries: list[str] = []
        round_started = time.monotonic()
        for step in plan.steps:
            if not step.approved or step.step_id in used_steps:
                continue
            if stopped is not None and stopped():
                stop_reason = "STOPPED"
                break
            if time.monotonic() - started >= plan.max_duration_seconds:
                stop_reason = "LIMIT_REACHED"
                break
            if (
                len(round_queries) >= plan.max_queries_per_round
                or queries >= plan.max_total_results
            ):
                break
            used_steps.add(step.step_id)
            round_queries.append(step.query)
            queries += 1
            remaining = plan.max_total_results - len(discoveries)
            if remaining <= 0:
                break
            response = searcher.search_response(
                SearchRequest(
                    query=step.query,
                    mode=step.retrieval_strategy,
                    top_k=min(step.max_results, remaining),
                )
            )
            for result in response.results:
                if result.case_id in seen_cases:
                    duplicate_cases.append(result.case_id)
                    if result.relevant_passage.passage_id in seen_passages:
                        continue
                    new_passages.append(result.relevant_passage.passage_id)
                else:
                    new_cases.append(result.case_id)
                seen_cases.add(result.case_id)
                seen_passages.add(result.relevant_passage.passage_id)
                discoveries.append(
                    ResearchDiscovery(
                        step_id=step.step_id,
                        gap_id=step.gap_id,
                        intent=step.intent,
                        round_number=round_number,
                        result=result,
                    )
                )
        coverage = assess_coverage(
            matter, claim, finding, discoveries, queries_executed=queries
        )
        rounds.append(
            ResearchRound(
                number=round_number,
                queries=round_queries,
                new_case_ids=list(dict.fromkeys(new_cases)),
                new_passage_ids=list(dict.fromkeys(new_passages)),
                duplicate_case_ids=list(dict.fromkeys(duplicate_cases)),
                resolved_gap_ids=[
                    old.gap_id
                    for old in assess_coverage(matter, claim, finding).gaps
                    if old.type not in {gap.type for gap in coverage.gaps}
                ],
                elapsed_ms=int((time.monotonic() - round_started) * 1000),
            )
        )
        run.rounds = rounds.copy()
        run.discoveries = discoveries.copy()
        run.coverage = coverage
        if progress is not None:
            progress(run)
        if stopped is not None and stopped():
            stop_reason = "STOPPED"
        if stop_reason == "STOPPED":
            break
        if not new_cases and not new_passages:
            stop_reason = "NO_NOVELTY"
            break
        if not coverage.gaps:
            stop_reason = "GAPS_RESOLVED"
            break
        if not any(
            step.approved and step.step_id not in used_steps for step in plan.steps
        ):
            break
    run.rounds = rounds
    run.discoveries = discoveries
    run.coverage = coverage
    run.stop_reason = stop_reason
    run.status = "stopped" if stop_reason == "STOPPED" else "completed"
    run.completed_at = datetime.now(UTC)
    run.elapsed_ms = int((time.monotonic() - started) * 1000)
    run.queries_executed = queries
    run.new_authorities = len(
        {case_id for row in rounds for case_id in row.new_case_ids}
    )
    run.duplicate_authorities = len(
        {case_id for row in rounds for case_id in row.duplicate_case_ids}
    )
    run.gaps_resolved = len(
        {gap_id for row in rounds for gap_id in row.resolved_gap_ids}
    )
    if stop_reason == "LIMIT_REACHED":
        run.warning = (
            "LIMIT_REACHED: Research remains incomplete within configured bounds."
        )
    return run

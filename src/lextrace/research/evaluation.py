"""Deterministic engineering metrics for completed research runs."""

from collections.abc import Sequence
from typing import Protocol

from pydantic import Field

from lextrace.research.contracts import ResearchResponse
from lextrace.retrieval.contracts import Record


class QualityEvaluator(Protocol):
    """Optional future human or judge adapter; grounding never depends on it."""

    def evaluate(self, response: ResearchResponse) -> dict[str, float]: ...


class ResearchEvaluation(Record):
    runs: int
    citation_existence_rate: float
    passage_existence_rate: float
    claim_support_rate: float
    unsupported_claim_rate: float
    completion_rate: float
    tool_error_rate: float
    revision_rate: float
    mean_latency_seconds: float
    llm_calls: int
    input_tokens: int
    output_tokens: int
    known_cost: float | None
    relevant_case_coverage: float | None = None
    optional_quality: list[dict[str, float]] = Field(default_factory=list)


def evaluate_runs(
    responses: Sequence[ResearchResponse],
    *,
    relevant_case_ids: dict[str, set[str]] | None = None,
    quality_evaluator: QualityEvaluator | None = None,
) -> ResearchEvaluation:
    claims = [claim for run in responses for claim in run.claims if claim.substantive]
    verifications = [item for run in responses for item in run.verification_results]
    evidence_cases = {
        run.run_id: {item.result.case_id for item in run.relevant_cases}
        for run in responses
    }
    evidence_passages = {
        run.run_id: {
            item.result.relevant_passage.passage_id for item in run.relevant_cases
        }
        for run in responses
    }
    cited = sum(
        all(case_id in evidence_cases[run.run_id] for case_id in claim.cited_case_ids)
        for run in responses
        for claim in run.claims
    )
    passages = sum(
        all(pid in evidence_passages[run.run_id] for pid in claim.cited_passage_ids)
        for run in responses
        for claim in run.claims
    )
    supported = sum(item.status == "SUPPORTED" for item in verifications)
    unsupported = sum(
        item.status in {"UNSUPPORTED", "CONTRADICTED", "INSUFFICIENT_EVIDENCE"}
        for item in verifications
    )
    coverage: list[float] = []
    if relevant_case_ids is not None:
        for run in responses:
            labels = relevant_case_ids.get(run.run_id, set())
            if labels:
                coverage.append(len(labels & evidence_cases[run.run_id]) / len(labels))
    costs = [run.trace.usage.cost for run in responses]
    known_cost = (
        sum(cost for cost in costs if cost is not None)
        if any(cost is not None for cost in costs)
        else None
    )
    return ResearchEvaluation(
        runs=len(responses),
        citation_existence_rate=cited / len(claims) if claims else 0,
        passage_existence_rate=passages / len(claims) if claims else 0,
        claim_support_rate=supported / len(verifications) if verifications else 0,
        unsupported_claim_rate=unsupported / len(verifications) if verifications else 0,
        completion_rate=sum(run.trace.status == "completed" for run in responses)
        / len(responses)
        if responses
        else 0,
        tool_error_rate=sum(bool(run.trace.tool_errors) for run in responses)
        / len(responses)
        if responses
        else 0,
        revision_rate=sum(run.trace.revision_count > 0 for run in responses)
        / len(responses)
        if responses
        else 0,
        mean_latency_seconds=sum(run.trace.total_seconds for run in responses)
        / len(responses)
        if responses
        else 0,
        llm_calls=sum(run.trace.usage.calls for run in responses),
        input_tokens=sum(run.trace.usage.input_tokens for run in responses),
        output_tokens=sum(run.trace.usage.output_tokens for run in responses),
        known_cost=known_cost,
        relevant_case_coverage=sum(coverage) / len(coverage) if coverage else None,
        optional_quality=[quality_evaluator.evaluate(run) for run in responses]
        if quality_evaluator
        else [],
    )

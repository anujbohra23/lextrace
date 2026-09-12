"""Offline research-run evaluation metrics."""

import json
from pathlib import Path
from typing import Literal

from lextrace.research.contracts import (
    GroundingSummary,
    ResearchResponse,
    ResearchTrace,
    Usage,
)
from lextrace.research.evaluation import evaluate_runs


def empty_response(
    *, status: Literal["completed", "degraded", "failed"] = "completed"
) -> ResearchResponse:
    return ResearchResponse.model_validate(
        {
            "run_id": "run-1",
            "identified_issues": [],
            "research_plan": [],
            "relevant_cases": [],
            "citation_relationships": [],
            "case_analyses": [],
            "supporting_arguments": [],
            "opposing_arguments": [],
            "final_memo": None,
            "claims": [],
            "verification_results": [],
            "grounding_summary": GroundingSummary(
                total_substantive_claims=0,
                supported=0,
                partially_supported=0,
                unsupported=0,
                contradicted=0,
                insufficient_evidence=0,
                support_rate=0,
                confidence="INSUFFICIENT_EVIDENCE",
            ),
            "trace": ResearchTrace(
                run_id="run-1",
                status=status,
                nodes_executed=[],
                node_seconds={},
                node_usage={},
                retrieval_trace_ids=[],
                prompts=[],
                provider="fake",
                model="fake",
                tool_errors=[] if status == "completed" else ["safe error"],
                retry_count=0,
                evidence_count=0,
                claims_generated=0,
                claims_supported=0,
                revision_count=0,
                usage=Usage(calls=2, input_tokens=10, output_tokens=4),
                total_seconds=0.25,
            ),
        }
    )


def test_empty_and_workflow_metrics() -> None:
    metrics = evaluate_runs([empty_response(), empty_response(status="degraded")])
    assert metrics.runs == 2
    assert metrics.completion_rate == 0.5
    assert metrics.tool_error_rate == 0.5
    assert metrics.llm_calls == 4
    assert metrics.input_tokens == 20
    assert metrics.mean_latency_seconds == 0.25
    assert metrics.known_cost is None


def test_optional_quality_interface() -> None:
    class Quality:
        def evaluate(self, response: ResearchResponse) -> dict[str, float]:
            return {"clarity": 1.0}

    metrics = evaluate_runs([empty_response()], quality_evaluator=Quality())
    assert metrics.optional_quality == [{"clarity": 1.0}]


def test_golden_scenarios_are_versioned() -> None:
    data = json.loads(Path("tests/fixtures/research_golden.json").read_text())
    assert data["version"] == "research-golden-v1"
    assert {item["expected"] for item in data["scenarios"]} == {
        "SUPPORTED",
        "PARTIALLY_SUPPORTED",
        "UNSUPPORTED",
        "CONTRADICTED",
        "INSUFFICIENT_EVIDENCE",
    }

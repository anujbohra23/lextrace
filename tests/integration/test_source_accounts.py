"""Deterministic full API smoke before spending the single live provider run."""

import json
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from lextrace.investigations.contracts import EvidenceReference
from lextrace.investigations.findings import ALL_DIMENSIONS, ALL_EXPLANATIONS
from lextrace.investigations.inconsistency_investigation import evidence_id
from lextrace.research.prompts import PromptDefinition
from scripts.pilot_smoke import FixtureLLM, smoke

Output = TypeVar("Output", bound=BaseModel)


class UnknownReasoningFixture(FixtureLLM):
    """A complete schema response with uncertainty; never fabricates entailment."""

    def generate(
        self, prompt: PromptDefinition, schema: type[Output], context: dict[str, object]
    ) -> Output:
        if "text" in context:
            return super().generate(prompt, schema, context)
        self.usage.calls += 1
        accounts: Any = context["accounts"]
        evidence: Any = context["evidence"]
        premises = []
        for side, account in zip(("left", "right"), accounts, strict=True):
            reference = EvidenceReference.model_validate_json(
                json.dumps(account["original"]["evidence_references"][0])
            )
            for dimension in ALL_DIMENSIONS:
                premises.append(
                    dict(
                        side=side,
                        dimension=dimension,
                        entailment="UNKNOWN",
                        evidence_ids=[evidence_id(reference)],
                    )
                )
        return schema.model_validate_json(
            json.dumps(
                dict(
                    status="INSUFFICIENT_EVIDENCE",
                    premises=premises,
                    checks=[
                        dict(dimension=d, assessment="UNKNOWN", evidence_ids=[])
                        for d in ALL_DIMENSIONS
                    ],
                    alternatives=[
                        dict(explanation=e, assessment="UNKNOWN", evidence_ids=[])
                        for e in ALL_EXPLANATIONS
                    ],
                    evidence=[
                        dict(evidence_id=k, role="CONTEXT", entailment="UNKNOWN")
                        for k in evidence
                    ],
                )
            )
        )


def test_structured_accounts_full_workflow_keeps_uncertainty(tmp_path: Path) -> None:
    result = smoke(tmp_path, UnknownReasoningFixture(), with_findings=True)
    assert result.get("remaining_stages", "").startswith("PASS"), result
    assert result["source_grounded_witness_accounts"] == 2
    assert result["candidate_pairs"] == result["finding_count"] == 1
    assert result["reasoning_calls"] == 2
    assert result["reasoning_searches"] == 1
    assert result["finding_verification"] == ["INSUFFICIENT"]
    assert result["calls_after_findings"] == 5
    for stage in ("evidence_gate_retry", "findings_gate", "docx_readback", "restart"):
        assert result[stage] == "PASS"

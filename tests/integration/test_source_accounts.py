"""Deterministic full API smoke before spending the single live provider run."""

import json
from pathlib import Path
from typing import TypeVar, cast

from pydantic import BaseModel

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
        return schema.model_validate_json(
            json.dumps(
                dict(
                    status="INSUFFICIENT_EVIDENCE",
                    premise_entailments=["UNKNOWN"] * 20,
                    comparison_assessments=["UNKNOWN"] * 10,
                    comparison_evidence=[[] for _ in range(10)],
                    alternative_assessments=["UNKNOWN"] * 7,
                    alternative_evidence=[[] for _ in range(7)],
                    evidence_roles=["CONTEXT"]
                    * len(cast(list[object], context["evidence"])),
                    evidence_entailments=["UNKNOWN"]
                    * len(cast(list[object], context["evidence"])),
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

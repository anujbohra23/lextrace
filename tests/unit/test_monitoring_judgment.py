"""Evidence-bound generation and one repair, without live provider access."""

import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from lextrace.matter.monitoring import (
    ImpactJudgment,
    MonitoringEvidence,
    _passage_result,
)
from lextrace.matter.monitoring_judge import StructuredImpactJudge
from lextrace.research.ollama import OllamaLLM
from tests.unit.test_monitoring import case, matter_store


@pytest.mark.parametrize(
    "category",
    [
        "STRENGTHENS",
        "WEAKENS",
        "CREATES_CONFLICT",
        "RESOLVES_GAP",
        "NEW_RELEVANT_AUTHORITY",
    ],
)
def test_positive_judgment_requires_passage_reference(category: str) -> None:
    with pytest.raises(ValidationError):
        ImpactJudgment.model_validate(
            dict(
                claim_id="claim",
                case_id="1",
                passage_id="passage",
                category=category,
                exact_quote="quote",
                explanation="explanation",
                evidence_ids=[],
            )
        )


@pytest.mark.parametrize("outcome", ["repaired", "invalid", "no_effect"])
def test_one_reference_repair(tmp_path: Path, outcome: str) -> None:
    store, matter_id, claim_id = matter_store(tmp_path)
    claim = store.claim(matter_id, claim_id)
    assert claim is not None
    result = _passage_result(
        case("2", "The employee received notice before termination."), claim, 1
    )
    assert result is not None
    evidence = MonitoringEvidence(
        "run",
        claim,
        store.finding(matter_id, claim_id),
        result,
        "SAME_COURT",
        None,
        None,
        None,
        [],
    )
    requests = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        payload = json.loads(request.content)
        context = payload["messages"][1]["content"]
        assert result.relevant_passage.passage_id in context
        assert "evidence_ledger" in context
        if requests == 2:
            assert "previous_draft" in context
            assert "only repair attempt" in payload["messages"][0]["content"]
        draft = dict(
            claim_id=claim_id,
            case_id="2",
            passage_id=result.relevant_passage.passage_id,
            category="NO_MATERIAL_EFFECT" if outcome == "no_effect" else "WEAKENS",
            exact_quote="employee received notice",
            explanation="Synthetic evidence judgment.",
            evidence_ids=[result.relevant_passage.passage_id]
            if requests == 2 and outcome == "repaired"
            else [],
        )
        return httpx.Response(200, json={"message": {"content": json.dumps(draft)}})

    provider = OllamaLLM("fake-offline", transport=httpx.MockTransport(respond))
    try:
        judge = StructuredImpactJudge(provider)
        if outcome == "invalid":
            with pytest.raises(ValidationError, match="passage evidence reference"):
                judge.judge(evidence)
        else:
            judgment = judge.judge(evidence)
            assert judgment.category == (
                "NO_MATERIAL_EFFECT" if outcome == "no_effect" else "WEAKENS"
            )
        assert requests == (1 if outcome == "no_effect" else 2)
    finally:
        provider.close()
        store.close()


def test_apply_invalidates_only_affected_assessment(tmp_path: Path) -> None:
    from lextrace.matter.contracts import MatterAnalysis
    from lextrace.matter.monitoring import MonitorService, apply_alert
    from lextrace.matter.monitoring_contracts import MonitoringLimits
    from lextrace.retrieval.documents import Corpus
    from tests.unit.test_monitoring_completion import CategoryJudge

    store, matter_id, claim_id = matter_store(tmp_path)
    claim = store.claim(matter_id, claim_id)
    finding = store.finding(matter_id, claim_id)
    assert claim is not None and finding is not None
    claim.verification_state = "SUPPORTED"
    finding.verification_status = "SUPPORTED"
    finding.vulnerability = "STRONG"
    other = claim.model_copy(update={"claim_id": "c" * 32, "importance": "low"})
    other_finding = finding.model_copy(update={"claim_id": other.claim_id})
    store.save_analysis(
        MatterAnalysis(
            matter_id=matter_id,
            document_id=claim.document_id,
            claims=[claim, other],
            findings=[finding, other_finding],
            issues=[],
            citations=[],
        )
    )
    before = store.finding(matter_id, other.claim_id)
    store.add_monitoring_target(matter_id, "CLAIM", claim_id)
    old = Corpus([case("1", "Old source.")])
    new = Corpus(
        old.cases + [case("2", "The employee received notice before termination.")]
    )
    MonitorService(
        store,
        old,
        new,
        judge=CategoryJudge("WEAKENS"),
        limits=MonitoringLimits(max_llm_calls=1),
    ).run([matter_id])
    alert = store.matter_alerts(matter_id)[0]
    assert apply_alert(store, matter_id, alert.alert_id).review_state == "APPLIED"
    updated = store.finding(matter_id, claim_id)
    assert updated is not None
    assert (
        updated.verification_status == updated.vulnerability == "INSUFFICIENT_EVIDENCE"
    )
    updated_claim = store.claim(matter_id, claim_id)
    assert updated_claim is not None
    assert updated_claim.verification_state == "INSUFFICIENT_EVIDENCE"
    assert any(e.result.case_id == "2" for e in updated.evidence)
    assert store.research_coverage(claim_id) is not None
    assert store.finding(matter_id, other.claim_id) == before
    assert apply_alert(store, matter_id, alert.alert_id).review_state == "APPLIED"
    assert store.finding(matter_id, claim_id) == updated
    store.close()

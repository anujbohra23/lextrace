"""Offline corpus-delta, bounded monitoring, review, and application checks."""

import json
from datetime import date
from pathlib import Path
from typing import Literal, cast

import pytest
from pydantic import BaseModel, HttpUrl

from lextrace.corpus import serialize_cases
from lextrace.domain.case import Case, Opinion
from lextrace.evaluation.benchmark import CitationEvidence, OpinionMapping
from lextrace.graph.build import build_graph
from lextrace.graph.contracts import GraphEvidenceBundle
from lextrace.graph.intelligence import PrecedentIntelligence, doctrine_for_claim
from lextrace.graph.store import CitationGraph
from lextrace.matter.contracts import (
    ArgumentFinding,
    ClaimAuthorityLink,
    LegalClaim,
    MatterAnalysis,
    MatterError,
    MatterEvidence,
    SourceSpan,
)
from lextrace.matter.monitoring import (
    ImpactJudgment,
    MonitoringEvidence,
    MonitorService,
    apply_alert,
)
from lextrace.matter.monitoring_contracts import (
    ImpactCategory,
    MonitoringLimits,
    MonitoringRun,
)
from lextrace.matter.monitoring_corpus import (
    corpus_delta,
    corpus_version,
    merge_corpus,
)
from lextrace.matter.research_contracts import ResearchCoverage, ResearchGap
from lextrace.matter.store import MatterStore
from lextrace.retrieval.documents import Corpus
from lextrace.retrieval.engine import LexTraceRetriever
from tests.unit.test_deep_research import result


def case(identifier: str, text: str, court: str = "ca2") -> Case:
    return Case(
        source_id=identifier,
        source_url=HttpUrl(f"https://www.courtlistener.com/opinion/{identifier}/"),
        name=f"Synthetic Case {identifier}",
        date_filed=date(2025, 1, 1),
        court_id=court,
        docket_number=f"25-{identifier}",
        opinions=[
            Opinion(
                source_id=str(int(identifier) + 1000),
                kind="combined",
                text=text,
                text_source_field="plain_text",
            )
        ],
    )


def matter_store(tmp_path: Path) -> tuple[MatterStore, str, str]:
    store = MatterStore(tmp_path / "matter.sqlite3", tmp_path / "private")
    matter = store.create_matter("Synthetic matter", court="ca2")
    doc = store.add_document(
        matter.matter_id,
        "brief.txt",
        b"The employee received notice before termination.",
    )
    span = SourceSpan(document_id=doc.document_id, section_id="s", start=0, end=48)
    claim = LegalClaim(
        claim_id="b" * 32,
        matter_id=matter.matter_id,
        document_id=doc.document_id,
        exact_source_text="The employee received notice before termination.",
        normalized_proposition="The employee received notice before termination.",
        span=span,
        importance="high",
    )
    finding = ArgumentFinding(
        claim_id=claim.claim_id,
        issue_id=None,
        proposition=claim.normalized_proposition,
        source=span,
        cited_authorities=[],
        evidence=[],
        counter_authorities=[],
        unresolved_citation_ids=[],
        verification_status="INSUFFICIENT_EVIDENCE",
        research_coverage="NO_RESULTS",
        vulnerability="INSUFFICIENT_EVIDENCE",
        explanation="No verified support.",
    )
    store.save_analysis(
        MatterAnalysis(
            matter_id=matter.matter_id,
            document_id=doc.document_id,
            issues=[],
            claims=[claim],
            citations=[],
            findings=[finding],
        )
    )
    return store, matter.matter_id, claim.claim_id


def citation_graph(tmp_path: Path, corpus: Corpus) -> CitationGraph:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    corpus_path = data / "graph-cases.jsonl"
    corpus_path.write_text(serialize_cases(corpus.cases), encoding="utf-8")
    bundle = GraphEvidenceBundle(
        citations=[
            CitationEvidence(
                citing_opinion_id="1002",
                cited_opinion_ids=["1001"],
                relation_source="opinions_cited",
                payload_sha256="a" * 64,
                complete=True,
            )
        ],
        opinion_mappings=[
            OpinionMapping(opinion_id="1001", case_id="1", payload_sha256="b" * 64),
            OpinionMapping(opinion_id="1002", case_id="2", payload_sha256="c" * 64),
        ],
        source_provenance="synthetic-offline-fixture",
    )
    source = data / "graph-evidence.json"
    source.write_text(bundle.model_dump_json(), encoding="utf-8")
    output = tmp_path / "artifacts" / "graphs" / "monitoring-fixture"
    build_graph(corpus_path, source, output)
    return CitationGraph(output)


def link_authority(store: MatterStore, matter_id: str, claim_id: str) -> None:
    finding = store.finding(matter_id, claim_id)
    assert finding is not None
    finding.evidence.append(
        MatterEvidence(
            evidence_id="case-1",
            claim_id=claim_id,
            role="cited",
            retrieval_query="notice",
            result=result("1"),
        )
    )
    finding.cited_authorities.append(
        ClaimAuthorityLink(
            claim_id=claim_id,
            case_id="1",
            relation="SUPPORTS",
            evidence_id="case-1",
        )
    )
    store.update_finding(finding)


class MonitoringGolden(BaseModel):
    id: str
    title: str
    new_text: str | None
    court: str
    judgment: Literal["SUPPORTS", "COUNTERS", "CONFLICTS", "NO_MATERIAL_EFFECT"] | None
    citation: bool
    gap: bool
    action: Literal["none", "confirm_treatment", "repeat", "dismiss_repeat", "apply"]
    expected_outcome: str
    expected_impact: str | None
    expected_severity: str | None
    expected_after_review_impact: str | None = None


_golden_file = Path(__file__).parents[1] / "fixtures" / "monitoring_golden.json"
MONITORING_GOLDEN = {
    item.id: item
    for item in (
        MonitoringGolden.model_validate(raw)
        for raw in json.loads(_golden_file.read_text())["scenarios"]
    )
}


class FakeJudge:
    def judge(self, evidence: MonitoringEvidence) -> ImpactJudgment:
        result = evidence.result
        return ImpactJudgment(
            claim_id=evidence.claim.claim_id,
            case_id=result.case_id,
            passage_id=result.relevant_passage.passage_id,
            category="WEAKENS",
            exact_quote="employee received notice",
            explanation="The synthetic passage disputes the claimed timing.",
            evidence_ids=[result.relevant_passage.passage_id],
        )


class ScenarioJudge:
    def __init__(
        self,
        category: Literal["SUPPORTS", "COUNTERS", "CONFLICTS", "NO_MATERIAL_EFFECT"],
    ) -> None:
        self.category = category

    def judge(self, evidence: MonitoringEvidence) -> ImpactJudgment:
        result = evidence.result
        category = cast(
            ImpactCategory,
            {
                "SUPPORTS": "STRENGTHENS",
                "COUNTERS": "WEAKENS",
                "CONFLICTS": "CREATES_CONFLICT",
                "NO_MATERIAL_EFFECT": "NO_MATERIAL_EFFECT",
            }[self.category],
        )
        return ImpactJudgment(
            claim_id=evidence.claim.claim_id,
            case_id=result.case_id,
            passage_id=result.relevant_passage.passage_id,
            category=category,
            exact_quote="employee received notice",
            explanation="Synthetic passage judgment; lawyer review is required.",
            evidence_ids=[result.relevant_passage.passage_id],
        )


@pytest.mark.parametrize("scenario", MONITORING_GOLDEN.values(), ids=MONITORING_GOLDEN)
def test_golden_monitoring_scenario(
    scenario: MonitoringGolden, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    store, matter_id, claim_id = matter_store(tmp_path)
    store.add_monitoring_target(matter_id, "CLAIM", claim_id)
    old = Corpus([case("1", "The employee received notice before termination.")])
    new = (
        Corpus(old.cases + [case("2", scenario.new_text, scenario.court)])
        if scenario.new_text is not None
        else old
    )
    graph: CitationGraph | None = None
    if scenario.citation:
        link_authority(store, matter_id, claim_id)
        store.add_monitoring_target(matter_id, "AUTHORITY", "1")
        graph = citation_graph(tmp_path, new)
    if scenario.gap:
        store.save_research_coverage(
            ResearchCoverage(
                claim_id=claim_id,
                issue_id=None,
                category="PARTIAL",
                gaps=[
                    ResearchGap(
                        gap_id="gap-controlling",
                        claim_id=claim_id,
                        type="NO_CONTROLLING_AUTHORITY",
                        explanation="No controlling support recorded.",
                        objective="Find controlling support.",
                        priority="HIGH",
                    )
                ],
            )
        )
    judge = ScenarioJudge(scenario.judgment) if scenario.judgment is not None else None
    limits = MonitoringLimits(max_llm_calls=1 if judge else 0)

    def check() -> MonitoringRun:
        return MonitorService(
            store, old, new, new_graph=graph, judge=judge, limits=limits
        ).run([matter_id])

    run = check()
    assert run.outcome == scenario.expected_outcome
    alerts = store.matter_alerts(matter_id)
    if scenario.expected_impact is None:
        assert alerts == []
    else:
        assert len(alerts) == 1
        alert = alerts[0]
        impact = store.change_impact(matter_id, alert.impact_id)
        assert impact is not None
        assert impact.category == scenario.expected_impact
        assert alert.severity == scenario.expected_severity
        if impact.category == "NEW_RELEVANT_AUTHORITY":
            with pytest.raises(MatterError, match="Review the legal effect"):
                apply_alert(store, matter_id, alert.alert_id)
        if scenario.action == "confirm_treatment":
            assert graph is not None
            event = store.change_event(matter_id, alert.event_id)
            assert event is not None and event.treatment is not None
            assert event.treatment.review_state == "UNREVIEWED"
            assert event.treatment.generated_label in {"LIMITS", "OVERRULES"}
            claim = store.claim(matter_id, claim_id)
            assert claim is not None
            trace = PrecedentIntelligence(LexTraceRetriever(new, graph=graph)).trace(
                "1",
                proposition=claim.normalized_proposition,
                backward_depth=0,
                forward_depth=1,
            )
            store.save_doctrine(
                matter_id,
                claim_id,
                "fixture-reviewed",
                doctrine_for_claim(claim_id, claim.normalized_proposition, trace),
            )
            store.review_treatment(
                matter_id, claim_id, event.treatment.annotation_id, "CONFIRMED"
            )
            check()
            revised = store.matter_alerts(matter_id)
            assert len(revised) == 1 and revised[0].version == 2
            assert revised[0].severity == "HIGH"
            final = store.change_impact(matter_id, revised[0].impact_id)
            assert final is not None
            assert final.category == scenario.expected_after_review_impact
        if scenario.action in {"repeat", "dismiss_repeat"}:
            if scenario.action == "dismiss_repeat":
                store.review_matter_alert(matter_id, alert.alert_id, "DISMISSED")
            check()
            again = store.matter_alerts(matter_id)
            assert len(again) == 1
            assert again[0].review_state == (
                "DISMISSED" if scenario.action == "dismiss_repeat" else "UNREAD"
            )
        if scenario.action == "apply":
            assert (
                apply_alert(store, matter_id, alert.alert_id).review_state == "APPLIED"
            )
            finding = store.finding(matter_id, claim_id)
            assert finding is not None
            assert any(item.result.case_id == "2" for item in finding.evidence)
            if scenario.expected_impact in {"WEAKENS", "CREATES_CONFLICT"}:
                assert any(
                    item.evidence.result.case_id == "2"
                    for item in finding.counter_authorities
                )
                assert any(
                    item.case_id == "2" and item.relation == "COUNTERS"
                    for item in finding.cited_authorities
                )
    if graph is not None:
        graph.close()
    store.close()


def test_monitoring_golden_catalog_is_complete() -> None:
    assert set(MONITORING_GOLDEN) == {
        "irrelevant_case",
        "persuasive_support",
        "controlling_support",
        "counter_authority",
        "limiting_case",
        "overruling_review",
        "citing_no_effect",
        "gap_resolved",
        "duplicate_run",
        "dismissed_alert",
        "applied_alert",
        "no_material_change",
    }


def test_corpus_delta_distinguishes_added_changed_and_duplicate() -> None:
    old = Corpus([case("1", "Existing opinion text.")])
    duplicate, duplicate_delta = merge_corpus(
        old, [case("1", "Existing opinion text.")]
    )
    assert duplicate.hash == old.hash
    assert duplicate_delta.added_case_ids == []
    revised, revised_delta = merge_corpus(old, [case("1", "Revised opinion text.")])
    assert revised_delta.added_case_ids == []
    assert revised_delta.text_changed_case_ids == ["1"]
    expanded, added_delta = merge_corpus(old, [case("2", "New opinion text.")])
    assert added_delta.added_case_ids == ["2"]
    assert corpus_delta(corpus_version(old), corpus_version(expanded)) == added_delta


def test_monitoring_no_change_and_verified_apply(tmp_path: Path) -> None:
    store, matter_id, claim_id = matter_store(tmp_path)
    target = store.add_monitoring_target(matter_id, "CLAIM", claim_id)
    assert target == store.add_monitoring_target(matter_id, "CLAIM", claim_id)
    old = Corpus([case("1", "Unrelated old opinion.")])
    unchanged = MonitorService(store, old, old).run([matter_id])
    assert unchanged.outcome == "NO_MATERIAL_CHANGE"
    assert store.matter_alerts(matter_id) == []
    new = Corpus(
        old.cases + [case("2", "The employee received notice before termination.")]
    )
    run = MonitorService(
        store,
        old,
        new,
        judge=FakeJudge(),
        limits=MonitoringLimits(max_llm_calls=1),
    ).run([matter_id])
    assert run.outcome == "ALERTS"
    assert run.alerts_created == 1
    alert = store.matter_alerts(matter_id)[0]
    assert alert.severity in {"HIGH", "MEDIUM"}
    repeat = MonitorService(
        store,
        old,
        new,
        judge=FakeJudge(),
        limits=MonitoringLimits(max_llm_calls=1),
    ).run([matter_id])
    assert repeat.alerts_created == 0
    assert len(store.matter_alerts(matter_id)) == 1
    applied = apply_alert(store, matter_id, alert.alert_id)
    assert applied.review_state == "APPLIED"
    assert store.finding(matter_id, claim_id) is not None
    assert any(
        evidence.result.case_id == "2"
        for evidence in store.finding(matter_id, claim_id).evidence  # type: ignore[union-attr]
    )
    assert store.research_coverage(claim_id) is not None
    store.close()


def test_citation_treatment_requires_human_confirmation(
    tmp_path: Path, monkeypatch: object
) -> None:
    from pytest import MonkeyPatch

    assert isinstance(monkeypatch, MonkeyPatch)
    monkeypatch.chdir(tmp_path)
    store, matter_id, claim_id = matter_store(tmp_path)
    link_authority(store, matter_id, claim_id)
    store.add_monitoring_target(matter_id, "AUTHORITY", "1")
    old = Corpus([case("1", "The employee received notice before termination.")])
    new = Corpus(
        old.cases
        + [
            case(
                "2",
                "We limit Synthetic Case 1. The employee received notice "
                "before termination.",
            )
        ]
    )
    graph = citation_graph(tmp_path, new)
    first = MonitorService(store, old, new, new_graph=graph).run([matter_id])
    assert first.alerts_created == 1
    alert = store.matter_alerts(matter_id)[0]
    event = store.change_event(matter_id, alert.event_id)
    assert event is not None and event.treatment is not None
    assert event.treatment.generated_label == "LIMITS"
    assert event.treatment.review_state == "UNREVIEWED"
    assert alert.severity == "INFORMATIONAL"
    claim = store.claim(matter_id, claim_id)
    assert claim is not None
    trace = PrecedentIntelligence(LexTraceRetriever(new, graph=graph)).trace(
        "1",
        proposition=claim.normalized_proposition,
        backward_depth=0,
        forward_depth=1,
    )
    store.save_doctrine(
        matter_id,
        claim_id,
        "synthetic-graph-review",
        doctrine_for_claim(claim_id, claim.normalized_proposition, trace),
    )
    store.review_treatment(
        matter_id, claim_id, event.treatment.annotation_id, "CONFIRMED"
    )
    repeated = MonitorService(store, old, new, new_graph=graph).run([matter_id])
    assert repeated.events_created > 0
    confirmed = store.matter_alerts(matter_id)
    assert len(confirmed) == 1
    assert confirmed[0].severity == "HIGH"
    assert confirmed[0].version == 2
    graph.close()
    store.close()

"""Offline completion checks for structured impact and graph-only monitoring."""

from pathlib import Path
from typing import cast

import pytest

from lextrace.corpus import serialize_cases
from lextrace.graph.build import build_graph
from lextrace.graph.contracts import GraphEvidenceBundle
from lextrace.graph.intelligence import PrecedentIntelligence, doctrine_for_claim
from lextrace.graph.store import CitationGraph
from lextrace.matter.monitoring import (
    ImpactJudgment,
    MonitoringEvidence,
    MonitorService,
    verify_change,
)
from lextrace.matter.monitoring_contracts import ImpactCategory, MonitoringLimits
from lextrace.matter.monitoring_judge import StructuredImpactJudge
from lextrace.research.llm import StructuredLLM
from lextrace.retrieval.documents import Corpus
from lextrace.retrieval.engine import LexTraceRetriever
from tests.unit.test_monitoring import (
    case,
    citation_graph,
    link_authority,
    matter_store,
)


class CategoryJudge:
    def __init__(self, category: ImpactCategory, *, invalid: bool = False) -> None:
        self.category = category
        self.invalid = invalid

    def judge(self, evidence: MonitoringEvidence) -> ImpactJudgment:
        passage = evidence.result.relevant_passage
        return ImpactJudgment(
            case_id=evidence.result.case_id,
            passage_id=passage.passage_id,
            category=self.category,
            exact_quote="employee received notice",
            explanation="The supplied passage may affect this synthetic claim.",
            evidence_ids=["invented-id" if self.invalid else passage.passage_id],
        )


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        ("STRENGTHENS", "STRENGTHENS"),
        ("WEAKENS", "WEAKENS"),
        ("CREATES_CONFLICT", "CREATES_CONFLICT"),
        ("NO_MATERIAL_EFFECT", None),
        ("INSUFFICIENT_EVIDENCE", None),
    ],
)
def test_structured_categories_and_noise(
    tmp_path: Path, category: ImpactCategory, expected: str | None
) -> None:
    store, matter_id, claim_id = matter_store(tmp_path)
    store.add_monitoring_target(matter_id, "CLAIM", claim_id)
    old = Corpus([case("1", "Old unrelated source.")])
    new = Corpus(
        old.cases + [case("2", "The employee received notice before termination.")]
    )
    run = MonitorService(
        store,
        old,
        new,
        judge=CategoryJudge(category),
        limits=MonitoringLimits(max_llm_calls=1),
    ).run([matter_id])
    alerts = store.matter_alerts(matter_id)
    assert (alerts[0].severity if alerts else None) == (
        "HIGH"
        if category in {"WEAKENS", "CREATES_CONFLICT"}
        else "LOW"
        if expected
        else None
    )
    impact = store.change_impact(matter_id, alerts[0].impact_id) if alerts else None
    assert (impact.category if impact else None) == expected
    assert run.outcome == ("ALERTS" if expected else "NO_MATERIAL_CHANGE")
    store.close()


def test_invalid_model_evidence_cannot_create_substantive_alert(tmp_path: Path) -> None:
    store, matter_id, claim_id = matter_store(tmp_path)
    store.add_monitoring_target(matter_id, "CLAIM", claim_id)
    old = Corpus([case("1", "Old unrelated source.")])
    new = Corpus(
        old.cases + [case("2", "The employee received notice before termination.")]
    )
    run = MonitorService(
        store,
        old,
        new,
        judge=CategoryJudge("WEAKENS", invalid=True),
        limits=MonitoringLimits(max_llm_calls=1),
    ).run([matter_id])
    assert run.rejected_impacts == 1
    assert store.matter_alerts(matter_id)[0].severity == "INFORMATIONAL"
    store.close()


@pytest.mark.parametrize("category", ["NO_MATERIAL_EFFECT", "INSUFFICIENT_EVIDENCE"])
def test_non_substantive_judgment_needs_no_evidence_ids(
    tmp_path: Path, category: ImpactCategory
) -> None:
    class NoEffectJudge(CategoryJudge):
        def judge(self, evidence: MonitoringEvidence) -> ImpactJudgment:
            return super().judge(evidence).model_copy(update={"evidence_ids": []})

    store, matter_id, claim_id = matter_store(tmp_path)
    store.add_monitoring_target(matter_id, "CLAIM", claim_id)
    old = Corpus([case("1", "Old unrelated source.")])
    new = Corpus(
        old.cases + [case("2", "The employee received notice before termination.")]
    )
    run = MonitorService(
        store,
        old,
        new,
        judge=NoEffectJudge(category),
        limits=MonitoringLimits(max_llm_calls=1),
    ).run([matter_id])
    assert run.rejected_impacts == 0
    assert run.outcome == "NO_MATERIAL_CHANGE"
    assert store.matter_alerts(matter_id) == []
    store.close()


def test_malformed_judgment_fails_safely(tmp_path: Path) -> None:
    class MalformedJudge:
        def judge(self, evidence: MonitoringEvidence) -> ImpactJudgment:
            raise ValueError("synthetic-private-text-must-not-surface")

    store, matter_id, claim_id = matter_store(tmp_path)
    store.add_monitoring_target(matter_id, "CLAIM", claim_id)
    old = Corpus([case("1", "Old unrelated source.")])
    new = Corpus(
        old.cases + [case("2", "The employee received notice before termination.")]
    )
    run = MonitorService(
        store,
        old,
        new,
        judge=MalformedJudge(),
        limits=MonitoringLimits(max_llm_calls=1),
    ).run([matter_id])
    assert run.alerts_created == 1
    assert store.matter_alerts(matter_id)[0].severity == "INFORMATIONAL"
    assert "synthetic-private-text" not in " ".join(run.errors)
    store.close()


def test_verification_rejects_wrong_case_passage_target_and_run(tmp_path: Path) -> None:
    store, matter_id, claim_id = matter_store(tmp_path)
    target = store.add_monitoring_target(matter_id, "CLAIM", claim_id)
    old = Corpus([case("1", "Old unrelated source.")])
    new_case = case("2", "The employee received notice before termination.")
    run = MonitorService(
        store,
        old,
        Corpus(old.cases + [new_case]),
        judge=CategoryJudge("WEAKENS"),
        limits=MonitoringLimits(max_llm_calls=1),
    ).run([matter_id])
    alert = store.matter_alerts(matter_id)[0]
    event = store.change_event(matter_id, alert.event_id)
    impact = store.change_impact(matter_id, alert.impact_id)
    assert event is not None and impact is not None
    assert verify_change(store, run, target, new_case, event, impact)
    assert not verify_change(
        store,
        run,
        target,
        new_case,
        event.model_copy(update={"new_case_id": "999"}),
        impact,
    )
    assert not verify_change(
        store,
        run,
        target,
        new_case,
        event.model_copy(update={"monitoring_run_id": "other"}),
        impact,
    )
    assert not verify_change(
        store,
        run,
        target.model_copy(update={"enabled": False}),
        new_case,
        event,
        impact,
    )
    assert not verify_change(
        store,
        run,
        target,
        new_case,
        event.model_copy(update={"judgment_evidence_ids": ["not-supplied"]}),
        impact,
    )
    assert event.passage is not None
    wrong = event.passage.model_copy(update={"opinion_id": "999"})
    assert not verify_change(
        store,
        run,
        target,
        new_case,
        event.model_copy(update={"passage": wrong}),
        impact,
    )
    store.close()


def test_graph_only_edge_material_and_repeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    store, matter_id, claim_id = matter_store(tmp_path)
    link_authority(store, matter_id, claim_id)
    store.add_monitoring_target(matter_id, "AUTHORITY", "1")
    corpus = Corpus(
        [
            case("1", "The employee received notice before termination."),
            case(
                "2",
                "We cite Synthetic Case 1. The employee received notice "
                "before termination.",
            ),
        ]
    )
    data = tmp_path / "data"
    data.mkdir()
    corpus_path = data / "cases.jsonl"
    corpus_path.write_text(serialize_cases(corpus.cases), encoding="utf-8")
    empty_source = data / "empty.json"
    empty_source.write_text(
        GraphEvidenceBundle(
            citations=[], opinion_mappings=[], source_provenance="synthetic"
        ).model_dump_json(),
        encoding="utf-8",
    )
    old_path = tmp_path / "artifacts/graphs/old"
    build_graph(corpus_path, empty_source, old_path)
    old_graph = CitationGraph(old_path)
    new_graph = citation_graph(tmp_path, corpus)
    try:
        run = MonitorService(
            store,
            corpus,
            corpus,
            old_graph=old_graph,
            new_graph=new_graph,
            judge=CategoryJudge("WEAKENS"),
            limits=MonitoringLimits(max_llm_calls=1),
        ).run([matter_id])
        assert run.new_cases_examined == 0
        assert run.graph_cases_examined == 1
        assert run.delta is not None and run.delta.citation_edges_added
        assert run.alerts_created == 1
        assert store.matter_alerts(matter_id)[0].severity == "HIGH"
        alert = store.matter_alerts(matter_id)[0]
        event = store.change_event(matter_id, alert.event_id)
        impact = store.change_impact(matter_id, alert.impact_id)
        target = store.monitoring_targets(matter_id)[0]
        assert event is not None and impact is not None
        if event.treatment is not None:
            bad_treatment = event.treatment.model_copy(update={"graph_id": "wrong"})
            assert not verify_change(
                store,
                run,
                target,
                corpus.by_id["2"],
                event.model_copy(update={"treatment": bad_treatment}),
                impact,
            )
        repeated = MonitorService(
            store,
            corpus,
            corpus,
            old_graph=new_graph,
            new_graph=new_graph,
        ).run([matter_id])
        assert repeated.outcome == "NO_MATERIAL_CHANGE"
        assert repeated.alerts_created == 0
        removed = MonitorService(
            store,
            corpus,
            corpus,
            old_graph=new_graph,
            new_graph=old_graph,
        ).run([matter_id])
        assert removed.delta is not None and removed.delta.citation_edges_removed
        assert removed.alerts_created == 0
    finally:
        old_graph.close()
        new_graph.close()
        store.close()


def test_graph_only_unrelated_edge_is_not_alerted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    store, matter_id, claim_id = matter_store(tmp_path)
    link_authority(store, matter_id, claim_id)
    corpus = Corpus(
        [
            case("1", "The employee received notice before termination."),
            case("2", "Unrelated admiralty navigation rules."),
        ]
    )
    graph = citation_graph(tmp_path, corpus)
    try:
        run = MonitorService(store, corpus, corpus, new_graph=graph).run([matter_id])
        assert run.delta is not None and run.delta.citation_edges_added
        assert run.outcome == "NO_MATERIAL_CHANGE"
        assert store.matter_alerts(matter_id) == []
    finally:
        graph.close()
        store.close()


def test_confirmed_treatment_without_new_corpus_or_graph_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    store, matter_id, claim_id = matter_store(tmp_path)
    link_authority(store, matter_id, claim_id)
    corpus = Corpus(
        [
            case("1", "The employee received notice before termination."),
            case(
                "2",
                "We limit Synthetic Case 1. The employee received notice "
                "before termination.",
            ),
        ]
    )
    graph = citation_graph(tmp_path, corpus)
    try:
        first = MonitorService(store, corpus, corpus, new_graph=graph).run([matter_id])
        assert first.alerts_created == 1
        alert = store.matter_alerts(matter_id)[0]
        event = store.change_event(matter_id, alert.event_id)
        assert event is not None and event.treatment is not None
        claim = store.claim(matter_id, claim_id)
        assert claim is not None
        trace = PrecedentIntelligence(LexTraceRetriever(corpus, graph=graph)).trace(
            "1",
            proposition=claim.normalized_proposition,
            backward_depth=0,
            forward_depth=1,
        )
        store.save_doctrine(
            matter_id,
            claim_id,
            "synthetic-doctrine",
            doctrine_for_claim(claim_id, claim.normalized_proposition, trace),
        )
        store.review_treatment(
            matter_id, claim_id, event.treatment.annotation_id, "CONFIRMED"
        )
        second = MonitorService(
            store, corpus, corpus, old_graph=graph, new_graph=graph
        ).run([matter_id])
        assert second.graph_cases_examined == 1
        revised = store.matter_alerts(matter_id)[0]
        assert revised.version == 2 and revised.severity == "HIGH"
    finally:
        graph.close()
        store.close()


def test_structured_adapter_uses_only_supplied_evidence(tmp_path: Path) -> None:
    class FakeProvider:
        provider = "fake"
        model = "synthetic"
        retries = 0
        from lextrace.research.contracts import Usage

        usage = Usage()

        def generate(
            self, prompt: object, schema: object, context: dict[str, object]
        ) -> ImpactJudgment:
            assert (
                context["claim_proposition"]
                == "The employee received notice before termination."
            )
            assert context["case_id"] == "2"
            assert "private_document" not in context
            return ImpactJudgment(
                case_id="2",
                passage_id=str(context["passage_id"]),
                category="STRENGTHENS",
                exact_quote="employee received notice",
                explanation="Supported by the supplied passage.",
                evidence_ids=[str(context["passage_id"])],
            )

    store, matter_id, claim_id = matter_store(tmp_path)
    claim = store.claim(matter_id, claim_id)
    assert claim is not None
    from lextrace.matter.monitoring import _passage_result

    result = _passage_result(
        case("2", "The employee received notice before termination."), claim, 1
    )
    assert result is not None
    evidence = MonitoringEvidence(
        run_id="synthetic-run",
        claim=claim,
        finding=store.finding(matter_id, claim_id),
        result=result,
        authority_relationship="SAME_COURT",
        treatment=None,
        doctrine=None,
        coverage=None,
        citation_provenance_ids=[],
    )
    assert (
        StructuredImpactJudge(cast(StructuredLLM, FakeProvider()))
        .judge(evidence)
        .category
        == "STRENGTHENS"
    )
    store.close()

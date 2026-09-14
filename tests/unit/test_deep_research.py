"""Offline coverage, bounded planning, attack provenance, and matrix checks."""

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, Field, HttpUrl

from lextrace.graph.contracts import CitationEdge, OpinionCitation
from lextrace.graph.intelligence_contracts import (
    ArgumentImpact,
    CitationContext,
    DoctrineAnalysis,
    DoctrineState,
    PrecedentTrace,
    TraceEdge,
    TreatmentAnnotation,
)
from lextrace.matter.analysis import EvidenceJudgments
from lextrace.matter.contracts import (
    ArgumentFinding,
    ClaimAuthorityLink,
    CounterAuthority,
    DocumentSection,
    LegalClaim,
    Matter,
    MatterEvidence,
    SourceSpan,
)
from lextrace.matter.deep_research import assess_coverage, execute_plan, propose_plan
from lextrace.matter.matrix import filter_matrix, matrix_csv, matrix_rows
from lextrace.matter.red_team import (
    attack_surface,
    compare_matter_facts,
    independent_counter_research,
    red_team_claim,
    verify_attack,
)
from lextrace.matter.research_contracts import DeepResearchRun, FactComparison
from lextrace.matter.store import MatterStore
from lextrace.retrieval.contracts import (
    Diagnostics,
    Passage,
    RetrievalResult,
    RetrievalTrace,
    SearchResponse,
)


class GoldenAuthority(BaseModel):
    case_id: str
    court: str
    relation: Literal["SUPPORTS", "PARTIALLY_SUPPORTS", "COUNTERS"]


class GoldenScenario(BaseModel):
    id: str
    title: str
    claim_text: str
    matter_section: str | None = None
    authorities: list[GoldenAuthority]
    unresolved_citation_ids: list[str] = Field(default_factory=list)
    verification: str
    queries_executed: int
    expected_coverage: str
    expected_gaps: list[str]
    expected_attacks: list[str]
    expected_stop: str


_golden_path = Path(__file__).parents[1] / "fixtures" / "deep_research_golden.json"
GOLDEN = {
    scenario.id: scenario
    for scenario in (
        GoldenScenario.model_validate(item)
        for item in json.loads(_golden_path.read_text())["scenarios"]
    )
}


@pytest.mark.parametrize("scenario", GOLDEN.values(), ids=GOLDEN)
def test_golden_research_scenario(scenario: GoldenScenario) -> None:
    matter, claim, finding = context()
    claim = claim.model_copy(update={"normalized_proposition": scenario.claim_text})
    finding = finding.model_copy(
        update={
            "proposition": scenario.claim_text,
            "unresolved_citation_ids": scenario.unresolved_citation_ids,
            "verification_status": scenario.verification,
            "research_coverage": "SEARCHED" if scenario.authorities else "NO_RESULTS",
        }
    )
    for authority in scenario.authorities:
        evidence = MatterEvidence(
            evidence_id=f"e-{authority.case_id}",
            claim_id=claim.claim_id,
            role="counter" if authority.relation == "COUNTERS" else "cited",
            retrieval_query="synthetic rule",
            result=result(authority.case_id, authority.court),
        )
        finding.evidence.append(evidence)
        if authority.relation == "COUNTERS":
            finding.counter_authorities.append(
                CounterAuthority(
                    claim_id=claim.claim_id,
                    evidence=evidence,
                    research_query="synthetic counter rule",
                )
            )
        else:
            finding.cited_authorities.append(
                ClaimAuthorityLink(
                    claim_id=claim.claim_id,
                    case_id=authority.case_id,
                    relation=authority.relation,
                    evidence_id=evidence.evidence_id,
                )
            )
    coverage = assess_coverage(
        matter, claim, finding, queries_executed=scenario.queries_executed
    )
    assert coverage.category == scenario.expected_coverage
    assert {gap.type for gap in coverage.gaps} == set(scenario.expected_gaps)
    attacks = red_team_claim(matter, claim, finding, coverage, None)
    deterministic_attack_types = {
        attack.attack_type for attack in attacks if attack.verified
    }
    assert deterministic_attack_types == set(scenario.expected_attacks) - {
        "LIMITED_PRECEDENT",
        "FACTUAL_CONTRADICTION",
        "DISTINGUISHABLE_FACTS",
    }
    assert [e.result.case_id for e in finding.evidence] == [
        authority.case_id for authority in scenario.authorities
    ]
    assert [e.result.relevant_passage.passage_id for e in finding.evidence] == [
        f"p-{authority.case_id}" for authority in scenario.authorities
    ]
    if scenario.expected_stop == "NOT_RUN":
        assert scenario.id != "no_novelty"
    else:
        assert scenario.id == "no_novelty"
        plan = propose_plan(matter, claim, coverage)
        plan.status = "APPROVED"
        plan.steps = [step.model_copy(update={"approved": True}) for step in plan.steps]

        class DuplicateSearch:
            def search_response(self, request: object) -> SearchResponse:
                return SearchResponse(
                    results=[result("1001")],
                    trace=RetrievalTrace(
                        request_id="golden-no-novelty",
                        mode="bm25",
                        corpus_hash="synthetic",
                        index_version="fixture-v1",
                        query_length=10,
                    ),
                )

        run = DeepResearchRun(
            run_id="d" * 32,
            plan_id=plan.plan_id,
            matter_id=matter.matter_id,
            claim_id=claim.claim_id,
            status="queued",
        )
        assert (
            execute_plan(
                DuplicateSearch(), matter, claim, finding, plan, run
            ).stop_reason
            == scenario.expected_stop
        )


def test_golden_catalog_has_all_requested_scenarios() -> None:
    assert set(GOLDEN) == {
        "strong_complete",
        "strong_incomplete",
        "weak_citation",
        "missing_controlling",
        "contrary_authority",
        "limiting_precedent",
        "factual_contradiction",
        "distinguishable_precedent",
        "unresolved_citation",
        "no_novelty",
        "prompt_injection",
    }
    assert all(scenario.title and scenario.claim_text for scenario in GOLDEN.values())
    assert {scenario.expected_stop for scenario in GOLDEN.values()} == {
        "NOT_RUN",
        "NO_NOVELTY",
    }


def context() -> tuple[Matter, LegalClaim, ArgumentFinding]:
    now = datetime.now(UTC)
    matter = Matter(
        matter_id="a" * 32,
        name="Offline matter",
        court="ca2",
        created_at=now,
        updated_at=now,
    )
    claim = LegalClaim(
        claim_id="b" * 32,
        matter_id=matter.matter_id,
        document_id="c" * 32,
        exact_source_text="The rule applies.",
        normalized_proposition="The rule applies.",
        span=SourceSpan(document_id="c" * 32, section_id="s", start=0, end=17),
    )
    finding = ArgumentFinding(
        claim_id=claim.claim_id,
        issue_id=None,
        proposition=claim.normalized_proposition,
        source=claim.span,
        cited_authorities=[],
        evidence=[],
        counter_authorities=[],
        unresolved_citation_ids=["unresolved"],
        verification_status="INSUFFICIENT_EVIDENCE",
        research_coverage="NO_RESULTS",
        vulnerability="INSUFFICIENT_EVIDENCE",
        explanation="No verified support.",
    )
    return matter, claim, finding


def result(case_id: str, court: str = "ca2") -> RetrievalResult:
    return RetrievalResult(
        case_id=case_id,
        rank=1,
        final_score=1.0,
        retrieval_method="bm25",
        case_name=f"Case {case_id}",
        court=court,
        date_filed=date(2010, 1, 1),
        reporter_citations=None,
        source_url=HttpUrl(f"https://www.courtlistener.com/opinion/{case_id}/"),
        relevant_passage=Passage(
            passage_id=f"p-{case_id}",
            opinion_id=f"o-{case_id}",
            start=0,
            end=20,
            text="The exact case passage.",
        ),
        diagnostics=Diagnostics(),
    )


class FakeSearch:
    calls: int = 0

    def search_response(self, request: object) -> SearchResponse:
        self.calls += 1
        return SearchResponse(
            results=[result("1")],
            trace=RetrievalTrace(
                request_id="fake",
                mode="bm25",
                corpus_hash="offline",
                index_version="test",
                query_length=10,
            ),
        )


def test_coverage_plan_approval_and_bounded_novelty() -> None:
    matter, claim, finding = context()
    coverage = assess_coverage(matter, claim, finding)
    assert coverage.category == "UNKNOWN"
    assert {gap.type for gap in coverage.gaps} >= {
        "NO_CONTROLLING_AUTHORITY",
        "NO_COUNTER_AUTHORITY",
        "UNRESOLVED_CITATION",
        "INSUFFICIENT_EVIDENCE",
    }
    assert "indexed corpus" in coverage.warning
    plan = propose_plan(matter, claim, coverage)
    assert plan.status == "DRAFT"
    search = FakeSearch()
    run = DeepResearchRun(
        run_id="d" * 32,
        plan_id=plan.plan_id,
        matter_id=matter.matter_id,
        claim_id=claim.claim_id,
        status="queued",
    )
    try:
        execute_plan(search, matter, claim, finding, plan, run)
    except ValueError:
        pass
    else:
        raise AssertionError("Unapproved plan executed")
    assert search.calls == 0
    plan.status = "APPROVED"
    plan.steps = [step.model_copy(update={"approved": True}) for step in plan.steps[:2]]
    plan.max_queries_per_round = 1
    finished = execute_plan(search, matter, claim, finding, plan, run)
    assert search.calls == 2
    assert len(finished.rounds) == 2
    assert finished.rounds[0].new_case_ids == ["1"]
    assert finished.rounds[1].duplicate_case_ids == ["1"]
    assert finished.stop_reason == "NO_NOVELTY"
    assert len(finished.discoveries) == 1


def test_coverage_categories_do_not_conflate_search_hits_with_support() -> None:
    matter, claim, finding = context()
    support = MatterEvidence(
        evidence_id="support",
        claim_id=claim.claim_id,
        role="independent_support",
        retrieval_query="rule",
        result=result("1"),
    )
    opposing = MatterEvidence(
        evidence_id="counter",
        claim_id=claim.claim_id,
        role="counter",
        retrieval_query="limits",
        result=result("2", "ca9"),
    )
    finding.evidence = [support, opposing]
    finding.cited_authorities = [
        ClaimAuthorityLink(
            claim_id=claim.claim_id,
            case_id="1",
            relation="SUPPORTS",
            evidence_id="support",
        )
    ]
    finding.counter_authorities = [
        CounterAuthority(
            claim_id=claim.claim_id,
            evidence=opposing,
            research_query="limits",
        )
    ]
    finding.unresolved_citation_ids = []
    finding.verification_status = "SUPPORTED"
    finding.research_coverage = "SEARCHED"
    assert (
        assess_coverage(matter, claim, finding, queries_executed=2).category
        == "SUFFICIENT"
    )
    finding.counter_authorities = []
    assert assess_coverage(matter, claim, finding).category == "PARTIAL"
    finding.cited_authorities = []
    assert assess_coverage(matter, claim, finding).category == "INSUFFICIENT"
    assert any(
        gap.type == "INSUFFICIENT_EVIDENCE"
        for gap in assess_coverage(matter, claim, finding).gaps
    )


def test_red_team_requires_exact_provenance() -> None:
    matter, claim, finding = context()
    evidence = MatterEvidence(
        evidence_id="e" * 32,
        claim_id=claim.claim_id,
        role="cited",
        retrieval_query="rule",
        result=result("1", "ca9"),
    )
    finding.evidence = [evidence]
    coverage = assess_coverage(matter, claim, finding)
    attacks = red_team_claim(matter, claim, finding, coverage, None)
    non_controlling = next(
        a for a in attacks if a.attack_type == "NON_CONTROLLING_AUTHORITY"
    )
    assert non_controlling.verified
    assert non_controlling.passage_ids == ["p-1"]
    invalid = non_controlling.model_copy(update={"passage_ids": ["made-up"]})
    assert not verify_attack(invalid, finding, None).verified
    assert all(a.verified for a in attack_surface(matter.matter_id, attacks).findings)


def test_citation_overstatement_requires_existing_partial_judgment() -> None:
    matter, claim, finding = context()
    evidence = MatterEvidence(
        evidence_id="e" * 32,
        claim_id=claim.claim_id,
        role="cited",
        retrieval_query="rule",
        result=result("1"),
    )
    finding.evidence = [evidence]
    finding.cited_authorities = [
        ClaimAuthorityLink(
            claim_id=claim.claim_id,
            case_id="1",
            relation="PARTIALLY_SUPPORTS",
            evidence_id=evidence.evidence_id,
        )
    ]
    attacks = red_team_claim(
        matter, claim, finding, assess_coverage(matter, claim, finding), None
    )
    overstatement = next(
        attack for attack in attacks if attack.attack_type == "CITATION_OVERSTATEMENT"
    )
    assert verify_attack(overstatement, finding, None, matter=matter).verified
    finding.cited_authorities = []
    assert not verify_attack(overstatement, finding, None, matter=matter).verified


def test_limiting_precedent_requires_confirmed_context() -> None:
    matter, claim, finding = context()
    context_passage = result("2").relevant_passage
    citation_context = CitationContext(
        citing_case_id="2",
        cited_case_id="1",
        citing_opinion_id="22",
        passage=context_passage,
        source_url=HttpUrl("https://www.courtlistener.com/opinion/2/"),
        matched_text="Case 1",
        confidence="EXACT_NAME",
    )
    annotation = TreatmentAnnotation(
        annotation_id="annotation",
        graph_id="graph",
        citing_case_id="2",
        cited_case_id="1",
        generated_label="LIMITS",
        passage=citation_context,
        method="explicit-language-v1",
        confidence="HIGH",
        review_requirement="REVIEW_REQUIRED",
        review_state="CONFIRMED",
        created_at=datetime.now(UTC),
    )
    edge = CitationEdge(
        citing_case_id="2",
        cited_case_id="1",
        supports=[
            OpinionCitation(
                citing_opinion_id="22",
                cited_opinion_id="11",
                provenance_id="a" * 64,
            )
        ],
        supporting_opinion_edge_count=1,
    )
    trace = PrecedentTrace(
        seed_case_id="1",
        graph_id="graph",
        nodes=[],
        edges=[TraceEdge(edge=edge, context=citation_context, treatment=annotation)],
        nodes_examined=2,
        edges_examined=1,
        context_recovered=1,
        treatment_specific=1,
        treatment_fallback=0,
        elapsed_ms=1,
    )
    doctrine = DoctrineAnalysis(
        claim_id=claim.claim_id,
        trace=trace,
        events=[],
        state=DoctrineState(
            proposition=claim.normalized_proposition,
            relevant_authority_ids=["2"],
            controlling_authority_ids=[],
            latest_relevant_case_id="2",
            supporting_annotation_ids=[],
            limiting_annotation_ids=["annotation"],
            contrary_annotation_ids=[],
            unresolved_conflicts=[],
            synthesis=[],
            coverage_warning="Local graph only.",
        ),
        impact=ArgumentImpact(
            claim_id=claim.claim_id,
            category="WEAKENS",
            explanation="Limited.",
            annotation_ids=["annotation"],
            research_gaps=[],
        ),
    )
    coverage = assess_coverage(matter, claim, finding)
    attack = next(
        a
        for a in red_team_claim(matter, claim, finding, coverage, None, doctrine)
        if a.attack_type == "LIMITED_PRECEDENT"
    )
    assert attack.attack_type in GOLDEN["limiting_precedent"].expected_attacks
    assert verify_attack(attack, finding, None, doctrine=doctrine).verified
    annotation.review_state = "UNCERTAIN"
    assert not verify_attack(attack, finding, None, doctrine=doctrine).verified


def test_independent_red_team_search_requires_structured_passage_judgment() -> None:
    matter, claim, finding = context()
    run = DeepResearchRun(
        run_id="d" * 32,
        plan_id="red-team",
        matter_id=matter.matter_id,
        claim_id=claim.claim_id,
        status="running",
    )

    class FakeVerifier:
        def generate(
            self, _prompt: object, _schema: object, _context: object
        ) -> EvidenceJudgments:
            return EvidenceJudgments.model_validate(
                {
                    "judgments": [
                        {
                            "evidence_id": "red-0",
                            "status": "CONTRADICTED",
                            "supporting_passage_ids": ["p-1"],
                            "explanation": "Offline judgment.",
                        }
                    ]
                }
            )

    attacks = independent_counter_research(
        FakeSearch(),
        claim,
        run,
        FakeVerifier(),  # type: ignore[arg-type]
    )
    assert len(run.discoveries) == 1
    assert len(attacks) == 1
    assert verify_attack(attacks[0], finding, run).verified
    assert independent_counter_research(FakeSearch(), claim, run) == []


def test_factual_attack_requires_exact_matter_quote_and_case_passage() -> None:
    matter, claim, finding = context()
    section = DocumentSection(
        section_id="section",
        document_id=claim.document_id,
        order=0,
        kind="paragraph",
        text=GOLDEN["factual_contradiction"].matter_section or "",
        span=SourceSpan(
            document_id=claim.document_id,
            section_id="section",
            start=100,
            end=136,
        ),
    )
    finding.evidence = [
        MatterEvidence(
            evidence_id="e",
            claim_id=claim.claim_id,
            role="cited",
            retrieval_query="rule",
            result=result("1"),
        )
    ]
    run = DeepResearchRun(
        run_id="d" * 32,
        plan_id="red-team",
        matter_id=matter.matter_id,
        claim_id=claim.claim_id,
        status="running",
    )

    class FakeComparison:
        def generate(
            self, _prompt: object, _schema: object, _context: object
        ) -> FactComparison:
            return FactComparison.model_validate(
                {
                    "items": [
                        {
                            "category": "FACTUAL_CONTRADICTION",
                            "section_id": "section",
                            "exact_quote": "preceded the complaint",
                            "explanation": "Timing differs.",
                            "uncertainty": "Review chronology.",
                        }
                    ]
                }
            )

    attacks = compare_matter_facts(
        claim,
        finding,
        [section],
        run,
        FakeComparison(),  # type: ignore[arg-type]
    )
    assert len(attacks) == 1
    assert attacks[0].attack_type in GOLDEN["factual_contradiction"].expected_attacks
    assert attacks[0].matter_spans[0].start == 113
    assert verify_attack(attacks[0], finding, run, [section]).verified
    assert not verify_attack(attacks[0], finding, run).verified

    class FakeDistinction:
        def generate(
            self, _prompt: object, _schema: object, _context: object
        ) -> FactComparison:
            return FactComparison.model_validate(
                {
                    "items": [
                        {
                            "category": "DISTINGUISHABLE_FACTS",
                            "section_id": "section",
                            "exact_quote": "decision preceded",
                            "case_id": "1",
                            "passage_id": "p-1",
                            "explanation": "Different timing.",
                            "uncertainty": "Review materiality.",
                        }
                    ]
                }
            )

    distinguished = compare_matter_facts(
        claim,
        finding,
        [section],
        run,
        FakeDistinction(),  # type: ignore[arg-type]
    )
    assert distinguished[0].attack_type == "DISTINGUISHABLE_FACTS"
    assert (
        distinguished[0].attack_type
        in GOLDEN["distinguishable_precedent"].expected_attacks
    )
    assert verify_attack(distinguished[0], finding, run, [section]).verified
    bad = distinguished[0].model_copy(update={"passage_ids": ["invented"]})
    assert not verify_attack(bad, finding, run, [section]).verified


def test_prompt_injection_text_does_not_bypass_quote_gate() -> None:
    matter, claim, finding = context()
    section = DocumentSection(
        section_id="untrusted",
        document_id=claim.document_id,
        order=0,
        kind="paragraph",
        text=GOLDEN["prompt_injection"].matter_section or "",
        span=SourceSpan(
            document_id=claim.document_id,
            section_id="untrusted",
            start=0,
            end=42,
        ),
    )
    run = DeepResearchRun(
        run_id="d" * 32,
        plan_id="red-team",
        matter_id=matter.matter_id,
        claim_id=claim.claim_id,
        status="running",
    )

    class MaliciousOutput:
        def generate(
            self, _prompt: object, _schema: object, _context: object
        ) -> FactComparison:
            return FactComparison.model_validate(
                {
                    "items": [
                        {
                            "category": "FACTUAL_CONTRADICTION",
                            "section_id": "untrusted",
                            "exact_quote": "A fabricated contradiction",
                            "explanation": "Invented.",
                            "uncertainty": "",
                        }
                    ]
                }
            )

    assert (
        compare_matter_facts(
            claim,
            finding,
            [section],
            run,
            MaliciousOutput(),  # type: ignore[arg-type]
        )
        == []
    )


def test_matrix_csv_and_claim_scoped_lock(tmp_path: Path) -> None:
    store = MatterStore(tmp_path / "matter.sqlite3", tmp_path / "private")
    matter = store.create_matter("=Spreadsheet formula", court="ca2")
    doc = store.add_document(matter.matter_id, "brief.txt", b"A legal claim applies.")
    text = store.document_text(matter.matter_id, doc.document_id)
    span = SourceSpan(
        document_id=doc.document_id,
        section_id="s",
        start=0,
        end=len(text),
    )
    claim = LegalClaim(
        claim_id="f" * 32,
        matter_id=matter.matter_id,
        document_id=doc.document_id,
        exact_source_text=text,
        normalized_proposition="=Dangerous formula",
        span=span,
    )
    with store._lock, store._db:
        store._db.execute(
            "INSERT INTO matter_claims VALUES (?,?,?,?)",
            (
                claim.claim_id,
                matter.matter_id,
                doc.document_id,
                claim.model_dump_json(),
            ),
        )
    locked = store.set_claim_lock(matter.matter_id, claim.claim_id, True)
    assert locked.wording_locked
    rows = matrix_rows(store, matter.matter_id)
    assert rows[0].coverage == "UNKNOWN"
    assert filter_matrix(rows, coverage="SUFFICIENT") == []
    csv_data = matrix_csv(rows, matter.name)
    assert "'=Spreadsheet formula" in csv_data
    assert "'=Dangerous formula" in csv_data
    store.close()

"""Small typed LangGraph boundaries for approved research and Red Team runs."""

from collections.abc import Callable
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from lextrace.graph.intelligence_contracts import DoctrineAnalysis
from lextrace.matter.contracts import (
    ArgumentFinding,
    DocumentSection,
    LegalClaim,
    Matter,
)
from lextrace.matter.deep_research import Searcher, assess_coverage, execute_plan
from lextrace.matter.red_team import (
    RedTeamSearcher,
    compare_matter_facts,
    independent_counter_research,
    red_team_claim,
    verify_attack,
)
from lextrace.matter.research_contracts import (
    AttackFinding,
    DeepResearchPlan,
    DeepResearchRun,
    ResearchCoverage,
)
from lextrace.research.llm import StructuredLLM


class DeepResearchState(TypedDict):
    matter: Matter
    claim: LegalClaim
    finding: ArgumentFinding
    plan: DeepResearchPlan
    run: DeepResearchRun
    coverage: ResearchCoverage | None


class RedTeamState(TypedDict):
    matter: Matter
    claim: LegalClaim
    finding: ArgumentFinding
    coverage: ResearchCoverage
    run: DeepResearchRun
    sections: list[DocumentSection]
    doctrine: DoctrineAnalysis | None
    attacks: list[AttackFinding]


def deep_research_graph(
    searcher: Searcher,
    *,
    stopped: Callable[[], bool] | None = None,
    progress: Callable[[DeepResearchRun], None] | None = None,
) -> Any:
    builder = StateGraph(DeepResearchState)

    def initial(state: DeepResearchState) -> dict[str, ResearchCoverage]:
        return {
            "coverage": assess_coverage(
                state["matter"], state["claim"], state["finding"]
            )
        }

    def gate(state: DeepResearchState) -> dict[str, object]:
        if state["plan"].status != "APPROVED":
            raise ValueError("Research plan requires human approval.")
        return {}

    def search(state: DeepResearchState) -> dict[str, DeepResearchRun]:
        return {
            "run": execute_plan(
                searcher,
                state["matter"],
                state["claim"],
                state["finding"],
                state["plan"],
                state["run"],
                stopped=stopped,
                progress=progress,
            )
        }

    def final(state: DeepResearchState) -> dict[str, ResearchCoverage | None]:
        return {"coverage": state["run"].coverage}

    builder.add_node("coverage_assessment", initial)
    builder.add_node("approval_gate", gate)
    builder.add_node("research_execution", search)
    builder.add_node("coverage_reassessment", final)
    builder.add_edge(START, "coverage_assessment")
    builder.add_edge("coverage_assessment", "approval_gate")
    builder.add_edge("approval_gate", "research_execution")
    builder.add_edge("research_execution", "coverage_reassessment")
    builder.add_edge("coverage_reassessment", END)
    return builder.compile()


def red_team_graph(
    searcher: RedTeamSearcher,
    *,
    llm: StructuredLLM | None = None,
) -> Any:
    builder = StateGraph(RedTeamState)

    def hypotheses(state: RedTeamState) -> dict[str, list[AttackFinding]]:
        return {
            "attacks": red_team_claim(
                state["matter"],
                state["claim"],
                state["finding"],
                state["coverage"],
                state["run"],
                state["doctrine"],
            )
        }

    def research(state: RedTeamState) -> dict[str, object]:
        new = independent_counter_research(searcher, state["claim"], state["run"], llm)
        return {"run": state["run"], "attacks": state["attacks"] + new}

    def factual(state: RedTeamState) -> dict[str, list[AttackFinding]]:
        return {
            "attacks": state["attacks"]
            + compare_matter_facts(
                state["claim"],
                state["finding"],
                state["sections"],
                state["run"],
                llm,
            )
        }

    def verify(state: RedTeamState) -> dict[str, list[AttackFinding]]:
        return {
            "attacks": [
                verify_attack(
                    attack,
                    state["finding"],
                    state["run"],
                    state["sections"],
                    state["matter"],
                    state["doctrine"],
                )
                for attack in state["attacks"]
            ]
        }

    builder.add_node("attack_hypotheses", hypotheses)
    builder.add_node("counter_research", research)
    builder.add_node("matter_evidence_check", factual)
    builder.add_node("attack_verification", verify)
    builder.add_edge(START, "attack_hypotheses")
    builder.add_edge("attack_hypotheses", "counter_research")
    builder.add_edge("counter_research", "matter_evidence_check")
    builder.add_edge("matter_evidence_check", "attack_verification")
    builder.add_edge("attack_verification", END)
    return builder.compile()

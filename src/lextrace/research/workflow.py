"""Bounded LangGraph workflow over existing LexTrace retrieval and graph services."""

import time
import uuid
from collections.abc import Callable
from typing import Any, Literal, TypedDict, cast

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field

from lextrace.domain.case import Case
from lextrace.graph.contracts import CitationNeighbor
from lextrace.graph.store import CitationGraph
from lextrace.research.contracts import (
    AnalysisOutput,
    Argument,
    ArgumentOutput,
    CaseAnalysis,
    Claim,
    ClaimsOutput,
    EvidenceItem,
    GroundingSummary,
    IssueOutput,
    LegalIssue,
    Memo,
    PlanOutput,
    ResearchError,
    ResearchRequest,
    ResearchResponse,
    ResearchTask,
    ResearchTrace,
    RevisionOutput,
    SynthesisOutput,
    Usage,
    VerificationOutput,
    VerificationResult,
)
from lextrace.research.llm import StructuredLLM
from lextrace.research.observability import NullObserver, ResearchObserver
from lextrace.research.prompts import PromptDefinition, PromptId, prompt
from lextrace.retrieval.contracts import (
    Mode,
    RetrievalError,
    SearchFilters,
    SearchRequest,
)
from lextrace.retrieval.engine import LexTraceRetriever


class ResearchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_issues: int = Field(default=4, ge=1, le=10)
    max_queries: int = Field(default=6, ge=1, le=20)
    max_cases_per_query: int = Field(default=8, ge=1, le=20)
    max_cases_analyzed: int = Field(default=8, ge=1, le=20)
    max_graph_cases: int = Field(default=5, ge=0, le=20)
    max_graph_neighbors: int = Field(default=10, ge=1, le=100)
    max_context_words: int = Field(default=5000, ge=100, le=50000)
    max_revision_count: int = Field(default=1, ge=0, le=1)
    default_mode: Mode = "citation_reranked"


class ResearchGraphState(TypedDict, total=False):
    request: ResearchRequest
    run_id: str
    issues: list[LegalIssue]
    tasks: list[ResearchTask]
    evidence: list[EvidenceItem]
    relationships: list[CitationNeighbor]
    analyses: list[CaseAnalysis]
    supporting: list[Argument]
    opposing: list[Argument]
    memo: Memo | None
    claims: list[Claim]
    verifications: list[VerificationResult]
    grounding: GroundingSummary
    errors: list[str]
    warnings: list[str]
    nodes: list[str]
    timings: dict[str, float]
    node_usage: dict[str, Usage]
    prompts: list[str]
    retrieval_trace_ids: list[str]
    revision_count: int


class CaseStore:
    def __init__(self, retriever: LexTraceRetriever) -> None:
        self._cases = retriever.corpus.by_id

    def get(self, case_id: str) -> Case | None:
        return self._cases.get(case_id)


def _evidence_context(
    evidence: list[EvidenceItem], budget: int
) -> list[dict[str, object]]:
    selected = []
    words = 0
    for item in sorted(
        evidence, key=lambda item: (item.result.rank, item.result.case_id)
    ):
        passage = item.result.relevant_passage
        size = len(passage.text.split())
        if words + size > budget:
            continue
        words += size
        result = item.result
        item_context: dict[str, object] = {
            "case_id": result.case_id,
            "case_name": result.case_name,
            "court": result.court,
            "date_filed": result.date_filed.isoformat() if result.date_filed else None,
            "reporter_citations": result.reporter_citations,
            "source_url": str(result.source_url),
            "passage_id": passage.passage_id,
            "opinion_id": passage.opinion_id,
            "passage_text": passage.text,
            "task_ids": item.task_ids,
        }
        selected.append(item_context)
    return selected


class ResearchWorkflow:
    def __init__(
        self,
        retriever: LexTraceRetriever,
        llm: StructuredLLM,
        *,
        graph: CitationGraph | None = None,
        config: ResearchConfig | None = None,
        observer: ResearchObserver | None = None,
    ) -> None:
        self.retriever = retriever
        self.graph = graph or retriever.graph
        self.case_store = CaseStore(retriever)
        self.llm = llm
        self.config = config or ResearchConfig()
        self.observer = observer or NullObserver()
        builder = StateGraph(ResearchGraphState)
        nodes = [
            ("issue_spotter", self._issue),
            ("research_planner", self._plan),
            ("retrieval", self._retrieve),
            ("citation_intelligence", self._citations),
            ("precedent_analyzer", self._analyze),
            ("supporting_argument", self._supporting),
            ("opposing_argument", self._opposing),
            ("synthesis", self._synthesize),
            ("claim_extraction", self._claims),
            ("verification", self._verify),
            ("revision", self._revise),
            ("final_verification", self._verify),
            ("finalize", self._finalize),
        ]
        for name, function in nodes:
            builder.add_node(name, cast(Any, self._timed(name, function)))
        builder.add_edge(START, nodes[0][0])
        for (left, _), (right, _) in zip(nodes, nodes[1:], strict=False):
            builder.add_edge(left, right)
        builder.add_edge(nodes[-1][0], END)
        self._graph = builder.compile()

    def _timed(
        self, name: str, function: Callable[[ResearchGraphState], dict[str, object]]
    ) -> Callable[[ResearchGraphState], dict[str, object]]:
        def run(state: ResearchGraphState) -> dict[str, object]:
            started = time.perf_counter()
            usage_before = self.llm.usage.model_copy()
            try:
                result = function(state)
            except (ResearchError, RetrievalError) as error:
                result = {"errors": [*state.get("errors", []), str(error)]}
                self.observer.emit(
                    "node.failure", {"run_id": state["run_id"], "node": name}
                )
            elapsed = time.perf_counter() - started
            result["nodes"] = [*state.get("nodes", []), name]
            result["timings"] = {
                **state.get("timings", {}),
                name: elapsed,
            }
            usage_after = self.llm.usage
            node_usage = Usage(
                calls=usage_after.calls - usage_before.calls,
                input_tokens=usage_after.input_tokens - usage_before.input_tokens,
                cached_input_tokens=(
                    usage_after.cached_input_tokens - usage_before.cached_input_tokens
                ),
                output_tokens=usage_after.output_tokens - usage_before.output_tokens,
                cost=(
                    usage_after.cost - usage_before.cost
                    if usage_after.cost is not None and usage_before.cost is not None
                    else None
                ),
            )
            result["node_usage"] = {
                **state.get("node_usage", {}),
                name: node_usage,
            }
            self.observer.emit(
                "node.completed",
                {
                    "run_id": state["run_id"],
                    "node": name,
                    "seconds": elapsed,
                    "llm_calls": node_usage.calls,
                },
            )
            return result

        return run

    def _call(
        self,
        state: ResearchGraphState,
        definition: PromptDefinition,
        schema: type[BaseModel],
        context: dict[str, object],
    ) -> BaseModel:
        state.setdefault("prompts", []).append(definition.identity)
        return self.llm.generate(definition, schema, context)

    def _issue(self, state: ResearchGraphState) -> dict[str, object]:
        output = self._call(
            state,
            prompt("issue-spotting"),
            IssueOutput,
            {
                "question": state["request"].question,
                "maximum_issues": self.config.max_issues,
            },
        )
        assert isinstance(output, IssueOutput)
        return {
            "issues": output.issues[: self.config.max_issues],
            "prompts": state["prompts"],
        }

    def _plan(self, state: ResearchGraphState) -> dict[str, object]:
        if not state.get("issues"):
            return {"tasks": []}
        output = self._call(
            state,
            prompt("research-planning"),
            PlanOutput,
            {
                "issues": [i.model_dump(mode="json") for i in state["issues"]],
                "maximum_queries": self.config.max_queries,
            },
        )
        assert isinstance(output, PlanOutput)
        valid_issues = {issue.issue_id for issue in state["issues"]}
        tasks = [task for task in output.tasks if task.issue_id in valid_issues]
        for task in tasks:
            if "retrieval_mode" not in task.model_fields_set:
                task.retrieval_mode = self.config.default_mode
            if task.retrieval_mode == "citation_reranked" and self.graph is None:
                task.retrieval_mode = "reranked"
        return {"tasks": tasks[: self.config.max_queries], "prompts": state["prompts"]}

    def _retrieve(self, state: ResearchGraphState) -> dict[str, object]:
        found: dict[str, EvidenceItem] = {}
        trace_ids = list(state.get("retrieval_trace_ids", []))
        request = state["request"]
        for task in state.get("tasks", []):
            mode = task.retrieval_mode or self.config.default_mode
            if mode == "citation_reranked" and self.graph is None:
                mode = "reranked"
            response = self.retriever.search_response(
                SearchRequest(
                    query=task.query,
                    mode=mode,
                    top_k=min(task.target_results, self.config.max_cases_per_query),
                    filters=SearchFilters(
                        courts=task.preferred_courts
                        or ([request.jurisdiction] if request.jurisdiction else []),
                        filed_before=task.filed_before or request.as_of_date,
                    ),
                )
            )
            trace_ids.append(response.trace.request_id)
            for result in response.results:
                if result.case_id in found:
                    found[result.case_id].task_ids.append(task.task_id)
                else:
                    found[result.case_id] = EvidenceItem(
                        result=result, task_ids=[task.task_id]
                    )
        evidence = sorted(
            found.values(),
            key=lambda item: (item.result.rank, int(item.result.case_id)),
        )[: state["request"].max_cases]
        return {"evidence": evidence, "retrieval_trace_ids": trace_ids}

    def _citations(self, state: ResearchGraphState) -> dict[str, object]:
        if self.graph is None:
            return {
                "relationships": [],
                "warnings": [*state.get("warnings", []), "Citation graph unavailable."],
            }
        relationships = []
        for item in state.get("evidence", [])[: self.config.max_graph_cases]:
            relationships.extend(
                self.graph.neighbors(
                    item.result.case_id,
                    direction="outgoing",
                    limit=self.config.max_graph_neighbors,
                    as_of_date=state["request"].as_of_date,
                )
            )
        return {"relationships": relationships}

    def _analyze(self, state: ResearchGraphState) -> dict[str, object]:
        context = _evidence_context(
            state.get("evidence", []), self.config.max_context_words
        )
        if not context:
            return {
                "analyses": [],
                "warnings": [*state.get("warnings", []), "No retrieved evidence."],
            }
        output = self._call(
            state,
            prompt("precedent-analysis"),
            AnalysisOutput,
            {
                "issues": [i.model_dump() for i in state.get("issues", [])],
                "evidence": context,
            },
        )
        assert isinstance(output, AnalysisOutput)
        passages = {
            item.result.relevant_passage.passage_id: item.result.case_id
            for item in state["evidence"]
        }
        analyses = [
            analysis
            for analysis in output.analyses
            if analysis.case_id in {e.result.case_id for e in state["evidence"]}
            and all(
                passages.get(passage_id) == analysis.case_id
                for passage_id in analysis.relevant_passage_ids
            )
        ][: self.config.max_cases_analyzed]
        return {"analyses": analyses, "prompts": state["prompts"]}

    def _argument(
        self,
        state: ResearchGraphState,
        identifier: PromptId,
        position: Literal["supporting", "opposing"],
    ) -> dict[str, object]:
        if not state.get("analyses"):
            return {position: []}
        output = self._call(
            state,
            prompt(identifier),
            ArgumentOutput,
            {
                "analyses": [a.model_dump() for a in state["analyses"]],
                "evidence": _evidence_context(
                    state["evidence"], self.config.max_context_words
                ),
            },
        )
        assert isinstance(output, ArgumentOutput)
        arguments = [
            argument
            for argument in output.arguments
            if argument.position == position
            and all(
                self._provenance_errors(claim, state) == [] for claim in argument.claims
            )
            and set(argument.supporting_case_ids)
            <= {item.result.case_id for item in state.get("evidence", [])}
            and set(argument.supporting_passage_ids)
            <= {
                item.result.relevant_passage.passage_id
                for item in state.get("evidence", [])
            }
        ]
        return {position: arguments, "prompts": state["prompts"]}

    def _supporting(self, state: ResearchGraphState) -> dict[str, object]:
        return self._argument(state, "supporting-argument", "supporting")

    def _opposing(self, state: ResearchGraphState) -> dict[str, object]:
        return self._argument(state, "opposing-argument", "opposing")

    def _synthesize(self, state: ResearchGraphState) -> dict[str, object]:
        if not state.get("analyses"):
            return {"memo": None}
        output = self._call(
            state,
            prompt("memo-synthesis"),
            SynthesisOutput,
            {
                "question": state["request"].question,
                "analyses": [a.model_dump() for a in state["analyses"]],
                "supporting": [a.model_dump() for a in state.get("supporting", [])],
                "opposing": [a.model_dump() for a in state.get("opposing", [])],
            },
        )
        assert isinstance(output, SynthesisOutput)
        return {"memo": output.memo, "prompts": state["prompts"]}

    def _claims(self, state: ResearchGraphState) -> dict[str, object]:
        memo = state.get("memo")
        if memo is None:
            return {"claims": []}
        output = self._call(
            state,
            prompt("claim-extraction"),
            ClaimsOutput,
            {
                "memo": memo.model_dump(),
                "evidence": _evidence_context(
                    state.get("evidence", []), self.config.max_context_words
                ),
            },
        )
        assert isinstance(output, ClaimsOutput)
        return {"claims": output.claims, "prompts": state["prompts"]}

    def _verify(self, state: ResearchGraphState) -> dict[str, object]:
        claims = state.get("claims", [])
        if not claims:
            return {"verifications": []}
        output = self._call(
            state,
            prompt("claim-verification"),
            VerificationOutput,
            {
                "claims": [c.model_dump() for c in claims],
                "evidence": _evidence_context(
                    state.get("evidence", []), self.config.max_context_words
                ),
            },
        )
        assert isinstance(output, VerificationOutput)
        proposed = {item.claim_id: item for item in output.results}
        results = []
        for claim in claims:
            errors = self._provenance_errors(claim, state)
            result = proposed.get(claim.claim_id)
            verifier_passages = set(result.supporting_passage_ids) if result else set()
            if (
                result is None
                or errors
                or not verifier_passages <= set(claim.cited_passage_ids)
            ):
                result = VerificationResult(
                    claim_id=claim.claim_id,
                    status="UNSUPPORTED",
                    support_score=0,
                    supporting_passage_ids=[],
                    explanation="Deterministic citation provenance validation failed.",
                    missing_support=errors,
                )
            results.append(result)
        return {"verifications": results, "prompts": state["prompts"]}

    def _provenance_errors(self, claim: Claim, state: ResearchGraphState) -> list[str]:
        evidence_by_passage = {
            item.result.relevant_passage.passage_id: item.result
            for item in state.get("evidence", [])
        }
        errors: list[str] = []
        if not claim.cited_passage_ids:
            errors.append("claim has no evidence passage")
        for case_id in claim.cited_case_ids:
            if self.case_store.get(case_id) is None:
                errors.append(f"case {case_id} is absent from the case store")
            retrieved_case_ids = {
                result.case_id for result in evidence_by_passage.values()
            }
            if case_id not in retrieved_case_ids:
                errors.append(f"case {case_id} was not retrieved in this run")
        for passage_id in claim.cited_passage_ids:
            result = evidence_by_passage.get(passage_id)
            if result is None:
                errors.append(f"passage {passage_id} was not retrieved in this run")
                continue
            if result.case_id not in claim.cited_case_ids:
                errors.append(f"passage {passage_id} belongs to another case")
            case = self.case_store.get(result.case_id)
            if case is None or not str(result.source_url):
                errors.append(f"case {result.case_id} has incomplete source metadata")
            elif (
                result.case_name != case.name
                or result.court != case.court_id
                or result.date_filed != case.date_filed
                or result.reporter_citations != case.reporter_citations
                or str(result.source_url) != str(case.source_url)
            ):
                errors.append(
                    f"case {result.case_id} metadata does not match its source"
                )
        return errors

    def _revise(self, state: ResearchGraphState) -> dict[str, object]:
        bad = {"UNSUPPORTED", "CONTRADICTED", "PARTIALLY_SUPPORTED"}
        if (
            state.get("memo") is None
            or not any(v.status in bad for v in state.get("verifications", []))
            or state.get("revision_count", 0) >= self.config.max_revision_count
        ):
            return {}
        memo = state.get("memo")
        assert memo is not None
        output = self._call(
            state,
            prompt("memo-revision"),
            RevisionOutput,
            {
                "memo": memo.model_dump(),
                "claims": [c.model_dump() for c in state.get("claims", [])],
                "verification": [
                    v.model_dump() for v in state.get("verifications", [])
                ],
            },
        )
        assert isinstance(output, RevisionOutput)
        return {
            "memo": output.memo,
            "claims": output.claims,
            "revision_count": state.get("revision_count", 0) + 1,
            "prompts": state["prompts"],
        }

    def _finalize(self, state: ResearchGraphState) -> dict[str, object]:
        counts = {
            status: 0
            for status in (
                "SUPPORTED",
                "PARTIALLY_SUPPORTED",
                "UNSUPPORTED",
                "CONTRADICTED",
                "INSUFFICIENT_EVIDENCE",
            )
        }
        for result in state.get("verifications", []):
            counts[result.status] += 1
        total = len(state.get("claims", []))
        supported = counts["SUPPORTED"]
        rate = supported / total if total else 0
        confidence: Literal["HIGH", "MODERATE", "LOW", "INSUFFICIENT_EVIDENCE"] = (
            "INSUFFICIENT_EVIDENCE"
            if not total
            else "HIGH"
            if rate >= 0.8 and counts["UNSUPPORTED"] + counts["CONTRADICTED"] == 0
            else "MODERATE"
            if rate >= 0.5
            else "LOW"
        )
        unresolved = counts["UNSUPPORTED"] + counts["CONTRADICTED"]
        warnings = list(state.get("warnings", []))
        memo = state.get("memo")
        if unresolved:
            warning = (
                f"{unresolved} substantive claim(s) remain unsupported or "
                "contradicted; they are identified in verification_results "
                "and must not be relied upon."
            )
            warnings.append(warning)
            if memo is not None and warning not in memo.uncertainties:
                memo = memo.model_copy(
                    update={"uncertainties": [*memo.uncertainties, warning]}
                )
        return {
            "memo": memo,
            "warnings": warnings,
            "grounding": GroundingSummary(
                total_substantive_claims=total,
                supported=supported,
                partially_supported=counts["PARTIALLY_SUPPORTED"],
                unsupported=counts["UNSUPPORTED"],
                contradicted=counts["CONTRADICTED"],
                insufficient_evidence=counts["INSUFFICIENT_EVIDENCE"],
                support_rate=rate,
                confidence=confidence,
            ),
        }

    def run(
        self, request: ResearchRequest, *, run_id: str | None = None
    ) -> ResearchResponse:
        started = time.perf_counter()
        usage_before = self.llm.usage.model_copy()
        retries_before = self.llm.retries
        run_id = run_id or uuid.uuid4().hex
        state = self._graph.invoke(
            {
                "request": request,
                "run_id": run_id,
                "errors": [],
                "warnings": [],
                "nodes": [],
                "timings": {},
                "node_usage": {},
                "prompts": [],
                "retrieval_trace_ids": [],
                "revision_count": 0,
            }
        )
        grounding = state.get("grounding") or GroundingSummary(
            total_substantive_claims=0,
            supported=0,
            partially_supported=0,
            unsupported=0,
            contradicted=0,
            insufficient_evidence=0,
            support_rate=0,
            confidence="INSUFFICIENT_EVIDENCE",
        )
        status: Literal["completed", "degraded", "failed"] = (
            "degraded" if state.get("errors") or state.get("warnings") else "completed"
        )
        trace = ResearchTrace(
            run_id=run_id,
            status=status,
            nodes_executed=state.get("nodes", []),
            node_seconds=state.get("timings", {}),
            node_usage=state.get("node_usage", {}),
            retrieval_trace_ids=state.get("retrieval_trace_ids", []),
            prompts=state.get("prompts", []),
            provider=self.llm.provider,
            model=self.llm.model,
            tool_errors=state.get("errors", []),
            retry_count=self.llm.retries - retries_before,
            evidence_count=len(state.get("evidence", [])),
            claims_generated=len(state.get("claims", [])),
            claims_supported=grounding.supported,
            revision_count=state.get("revision_count", 0),
            cache_hits=getattr(self.llm, "hits", 0),
            cache_misses=getattr(self.llm, "misses", 0),
            usage=Usage(
                calls=self.llm.usage.calls - usage_before.calls,
                input_tokens=self.llm.usage.input_tokens - usage_before.input_tokens,
                cached_input_tokens=(
                    self.llm.usage.cached_input_tokens
                    - usage_before.cached_input_tokens
                ),
                output_tokens=self.llm.usage.output_tokens - usage_before.output_tokens,
                cost=(
                    self.llm.usage.cost - usage_before.cost
                    if self.llm.usage.cost is not None and usage_before.cost is not None
                    else None
                ),
            ),
            total_seconds=time.perf_counter() - started,
        )
        self.observer.emit(
            "workflow.completed",
            {
                "run_id": run_id,
                "status": status,
                "evidence_count": trace.evidence_count,
                "claims_supported": trace.claims_supported,
                "seconds": trace.total_seconds,
            },
        )
        return ResearchResponse(
            run_id=run_id,
            identified_issues=state.get("issues", []),
            research_plan=state.get("tasks", []),
            relevant_cases=state.get("evidence", []),
            citation_relationships=state.get("relationships", []),
            case_analyses=state.get("analyses", []),
            supporting_arguments=state.get("supporting", []),
            opposing_arguments=state.get("opposing", []),
            final_memo=state.get("memo"),
            claims=state.get("claims", []),
            verification_results=state.get("verifications", []),
            grounding_summary=grounding,
            warnings=state.get("warnings", []),
            trace=trace,
        )

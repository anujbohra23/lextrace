"""Bounded, evidence-first intelligence over the existing citation graph."""

import hashlib
import re
import time
from datetime import UTC, date, datetime
from typing import Literal

from lextrace.graph.contracts import CitationEdge, GraphError, GraphNode
from lextrace.graph.courts import classify_authority
from lextrace.graph.intelligence_contracts import (
    ArgumentImpact,
    CitationContext,
    DoctrineAnalysis,
    DoctrineEvent,
    DoctrineState,
    EventCategory,
    GroundedStatement,
    ImpactCategory,
    PrecedentTrace,
    TraceEdge,
    TraceNode,
    TreatmentAnnotation,
    TreatmentLabel,
)
from lextrace.retrieval.contracts import Passage
from lextrace.retrieval.engine import LexTraceRetriever
from lextrace.retrieval.passages import segment, select

TRACE_VERSION = "precedent-trace-v1"
TREATMENT_VERSION = "explicit-language-v1"
_TREATMENT: tuple[tuple[TreatmentLabel, str], ...] = (
    ("OVERRULES", r"\boverrul(?:e|es|ed|ing)\b"),
    ("DISTINGUISHES", r"\bdistinguish(?:es|ed|ing)?\b"),
    ("LIMITS", r"\blimit(?:s|ed|ing)?\b"),
    ("CRITICIZES", r"\bcriticiz(?:e|es|ed|ing)\b"),
    ("FOLLOWS", r"\bfollow(?:s|ed|ing)?\b"),
    ("APPLIES", r"\bappl(?:y|ies|ied|ying)\b"),
    ("RELIES_ON", r"\brel(?:y|ies|ied|ying)\s+on\b"),
)


def _identity(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:24]


class PrecedentIntelligence:
    """Read a small neighborhood; never write into the immutable graph artifact."""

    def __init__(self, engine: LexTraceRetriever) -> None:
        if engine.graph is None:
            raise GraphError("Citation graph unavailable.")
        self.engine = engine
        self.graph = engine.graph

    def _passage(self, case_id: str, proposition: str) -> Passage | None:
        case = self.engine.corpus.by_id.get(case_id)
        if case is None:
            return None
        passages = segment(case, self.engine.config.passages)
        selected = select(passages, proposition, {}, 1)
        return selected[0] if selected and selected[0].score > 0 else None

    def _context(self, edge: CitationEdge) -> CitationContext | None:
        citing = self.engine.corpus.by_id.get(edge.citing_case_id)
        cited = self.engine.corpus.by_id.get(edge.cited_case_id)
        if citing is None or cited is None:
            return None
        candidates: list[tuple[str, Literal["EXACT_REPORTER", "EXACT_NAME"]]] = [
            (item, "EXACT_REPORTER")
            for item in cited.reporter_citations or []
            if sum(
                item.casefold()
                in {citation.casefold() for citation in case.reporter_citations or []}
                for case in self.engine.corpus.cases
            )
            == 1
        ]
        if (
            len(cited.name) >= 8
            and sum(
                case.name.casefold() == cited.name.casefold()
                for case in self.engine.corpus.cases
            )
            == 1
        ):
            candidates.append((cited.name, "EXACT_NAME"))
        opinion_ids = {support.citing_opinion_id for support in edge.supports}
        for opinion in citing.opinions:
            if opinion.source_id not in opinion_ids:
                continue
            for needle, confidence in candidates:
                match = re.search(re.escape(needle), opinion.text, re.IGNORECASE)
                if match is None:
                    continue
                passages = segment(citing, self.engine.config.passages)
                passage = next(
                    (
                        item
                        for item in passages
                        if item.opinion_id == opinion.source_id
                        and item.start <= match.start() < match.end() <= item.end
                    ),
                    None,
                )
                if passage is None:
                    continue
                return CitationContext(
                    citing_case_id=citing.source_id,
                    cited_case_id=cited.source_id,
                    citing_opinion_id=opinion.source_id,
                    passage=passage,
                    source_url=citing.source_url,
                    matched_text=opinion.text[match.start() : match.end()],
                    confidence=confidence,
                )
        return None

    def _treatment(
        self, edge: CitationEdge, context: CitationContext | None
    ) -> TreatmentAnnotation:
        label: TreatmentLabel = "UNKNOWN" if context is None else "CITES"
        if context is not None:
            passage = context.passage
            position = passage.text.lower().find(context.matched_text.lower())
            preceding = passage.text[max(0, position - 100) : position]
            # A verb must immediately govern this matched citation, not merely
            # occur elsewhere in the long opinion or evidence window.
            for candidate, pattern in _TREATMENT:
                match = list(re.finditer(pattern, preceding, re.IGNORECASE))
                if not match:
                    continue
                verb = match[-1]
                between = preceding[verb.end() :]
                before = preceding[max(0, verb.start() - 24) : verb.start()]
                if len(between.split()) > 8:
                    continue
                if re.search(r"\b(?:not|never|decline(?:s|d)?\s+to)\s*$", before, re.I):
                    continue
                if candidate == "OVERRULES" and re.match(r"\s+by\b", between, re.I):
                    continue
                label = candidate
                break
        specific = label not in ("UNKNOWN", "CITES")
        return TreatmentAnnotation(
            annotation_id=_identity(
                self.graph.metadata.graph_id,
                edge.citing_case_id,
                edge.cited_case_id,
                context.passage.passage_id if context else "none",
                TREATMENT_VERSION,
            ),
            graph_id=self.graph.metadata.graph_id,
            citing_case_id=edge.citing_case_id,
            cited_case_id=edge.cited_case_id,
            generated_label=label,
            passage=context,
            method="explicit-language-v1" if specific else "citation-only-v1",
            confidence="HIGH" if specific else "LOW" if context else "NONE",
            review_requirement="REVIEW_REQUIRED" if specific else "AUTO_CONFIRMED",
            created_at=datetime.now(UTC),
        )

    def trace(
        self,
        seed_case_id: str,
        *,
        proposition: str | None = None,
        forum_court: str | None = None,
        matter_id: str | None = None,
        as_of_date: date | None = None,
        backward_depth: int = 1,
        forward_depth: int = 1,
        max_nodes: int = 20,
        max_edges: int = 30,
    ) -> PrecedentTrace:
        if not (0 <= backward_depth <= 2 and 0 <= forward_depth <= 2):
            raise GraphError("Trace depth must be between zero and two.")
        if not (1 <= max_nodes <= 50 and 1 <= max_edges <= 100):
            raise GraphError("Trace size exceeds product bounds.")
        seed = self.graph.get_node(seed_case_id)
        if seed is None:
            raise GraphError("Seed case is not in the citation graph.")
        if as_of_date and (seed.date_filed is None or seed.date_filed > as_of_date):
            raise GraphError("Seed case is unavailable at the requested date.")
        started = time.perf_counter()
        seen: dict[str, GraphNode] = {seed_case_id: seed}
        found_edges: dict[tuple[str, str], CitationEdge] = {}
        examined = 0
        walks: tuple[tuple[Literal["outgoing", "incoming"], int], ...] = (
            ("outgoing", backward_depth),
            ("incoming", forward_depth),
        )
        for direction, depth in walks:
            frontier = [seed_case_id]
            visited = {seed_case_id}
            for _ in range(depth):
                following: list[str] = []
                for current in frontier:
                    if len(found_edges) >= max_edges:
                        break
                    for neighbor in self.graph.neighbors(
                        current,
                        direction=direction,
                        limit=max_edges,
                        as_of_date=as_of_date,
                    ):
                        if len(found_edges) >= max_edges:
                            break
                        examined += 1
                        node = neighbor.node
                        if node.case_id not in seen and len(seen) >= max_nodes:
                            continue
                        key = (
                            neighbor.edge.citing_case_id,
                            neighbor.edge.cited_case_id,
                        )
                        if key not in found_edges and len(found_edges) >= max_edges:
                            continue
                        seen[node.case_id] = node
                        found_edges[key] = neighbor.edge
                        if node.case_id not in visited:
                            visited.add(node.case_id)
                            following.append(node.case_id)
                frontier = sorted(following, key=int)
        nodes: list[TraceNode] = []
        for node in sorted(seen.values(), key=lambda item: int(item.case_id)):
            passage = self._passage(node.case_id, proposition) if proposition else None
            nodes.append(
                TraceNode(
                    node=node,
                    authority=classify_authority(
                        node.case_id, node.court, forum_court, matter_id=matter_id
                    ),
                    relevant_passage=passage,
                    relevance_score=passage.score if passage else None,
                    relevance_method="lexical-evidence-v1" if passage else None,
                )
            )
        if proposition:
            nodes.sort(
                key=lambda item: (
                    item.node.case_id != seed_case_id,
                    -(item.relevance_score or 0.0),
                    int(item.node.case_id),
                )
            )
        edges = []
        for edge in sorted(
            found_edges.values(),
            key=lambda item: (int(item.citing_case_id), int(item.cited_case_id)),
        ):
            context = self._context(edge)
            edges.append(
                TraceEdge(
                    edge=edge,
                    context=context,
                    treatment=self._treatment(edge, context),
                )
            )
        warnings = ["Retrieved local graph history is not a complete citator."]
        if proposition and any(node.relevant_passage is None for node in nodes):
            warnings.append("Some graph nodes lack proposition-matching local text.")
        if any(edge.context is None for edge in edges):
            warnings.append("Some citation contexts could not be recovered.")
        return PrecedentTrace(
            seed_case_id=seed_case_id,
            proposition=proposition,
            as_of_date=as_of_date,
            graph_id=self.graph.metadata.graph_id,
            nodes=nodes,
            edges=edges,
            nodes_examined=len(seen),
            edges_examined=examined,
            context_recovered=sum(edge.context is not None for edge in edges),
            treatment_specific=sum(
                edge.treatment.generated_label not in ("CITES", "UNKNOWN")
                for edge in edges
            ),
            treatment_fallback=sum(
                edge.treatment.generated_label in ("CITES", "UNKNOWN") for edge in edges
            ),
            elapsed_ms=round((time.perf_counter() - started) * 1000),
            warnings=warnings,
        )


def doctrine_for_claim(
    claim_id: str,
    proposition: str,
    trace: PrecedentTrace,
) -> DoctrineAnalysis:
    """Produce only statements whose annotation IDs point to exact passages."""
    nodes = {item.node.case_id: item for item in trace.nodes}
    events: list[DoctrineEvent] = []
    for item in trace.edges:
        annotation = item.treatment
        node = nodes.get(annotation.citing_case_id)
        if (
            annotation.generated_label in ("CITES", "UNKNOWN")
            or annotation.passage is None
            or node is None
            or node.node.date_filed is None
            or node.relevant_passage is None
            or annotation.review_state == "REJECTED"
        ):
            continue
        categories: dict[TreatmentLabel, EventCategory] = {
            "FOLLOWS": "RULE_ADOPTED",
            "APPLIES": "RULE_APPLIED",
            "RELIES_ON": "RULE_APPLIED",
            "DISTINGUISHES": "RULE_DISTINGUISHED",
            "LIMITS": "RULE_LIMITED",
            "CRITICIZES": "RULE_LIMITED",
            "OVERRULES": "RULE_OVERRULED",
        }
        category = categories[annotation.generated_label]
        events.append(
            DoctrineEvent(
                event_id=_identity(claim_id, annotation.annotation_id),
                category=category,
                case_id=node.node.case_id,
                case_name=node.node.case_name,
                court=node.node.court,
                date_filed=node.node.date_filed,
                proposition=proposition,
                supporting_passage=annotation.passage,
                annotation_id=annotation.annotation_id,
                uncertainty=["Treatment requires human review."]
                if annotation.review_requirement == "REVIEW_REQUIRED"
                and annotation.review_state != "CONFIRMED"
                else [],
            )
        )
    events.sort(key=lambda item: (item.date_filed, int(item.case_id), item.event_id))
    favorable = [
        event for event in events if event.category in ("RULE_ADOPTED", "RULE_APPLIED")
    ]
    adverse = [
        event
        for event in events
        if event.category in ("RULE_LIMITED", "RULE_DISTINGUISHED", "RULE_OVERRULED")
    ]
    later_annotations = {
        item.treatment.annotation_id
        for item in trace.edges
        if item.edge.cited_case_id == trace.seed_case_id
        and item.treatment.review_state == "CONFIRMED"
    }
    later_favorable = [
        event for event in favorable if event.annotation_id in later_annotations
    ]
    later_adverse = [
        event for event in adverse if event.annotation_id in later_annotations
    ]
    synthesis = [
        GroundedStatement(
            text=(
                f"{event.case_name or event.case_id} has explicit "
                f"{event.category.lower().replace('_', ' ')} language "
                "in a recovered citation context."
            ),
            annotation_ids=[event.annotation_id],
        )
        for event in events
    ]
    relevant_nodes = [
        node
        for node in trace.nodes
        if node.relevant_passage is not None and node.node.date_filed is not None
    ]
    controlling = sorted(
        {
            node.node.case_id
            for node in relevant_nodes
            if node.authority.category == "CONTROLLING"
        },
        key=int,
    )
    latest = max(
        relevant_nodes,
        key=lambda item: (item.node.date_filed or date.min, int(item.node.case_id)),
        default=None,
    )
    state = DoctrineState(
        proposition=proposition,
        relevant_authority_ids=sorted(
            {node.node.case_id for node in trace.nodes if node.relevant_passage},
            key=int,
        ),
        controlling_authority_ids=controlling,
        latest_relevant_case_id=latest.node.case_id if latest else None,
        supporting_annotation_ids=[event.annotation_id for event in favorable],
        limiting_annotation_ids=[event.annotation_id for event in adverse],
        contrary_annotation_ids=[event.annotation_id for event in adverse],
        unresolved_conflicts=[
            "Both supportive and limiting treatment appear in local evidence."
        ]
        if favorable and adverse
        else [],
        synthesis=synthesis,
        coverage_warning=(
            "Relevant retrieved doctrinal history only; local corpus and "
            "citation contexts are incomplete."
        ),
    )
    if later_favorable and later_adverse:
        impact: ImpactCategory = "MIXED"
    elif later_adverse:
        impact = "WEAKENS"
    elif later_favorable:
        impact = "STRENGTHENS"
    else:
        impact = "INSUFFICIENT_EVIDENCE"
    return DoctrineAnalysis(
        claim_id=claim_id,
        trace=trace,
        events=events,
        state=state,
        impact=ArgumentImpact(
            claim_id=claim_id,
            category=impact,
            explanation="Based on explicit local citation-context language."
            if later_favorable or later_adverse
            else "No confirmed later treatment of the seed authority is available.",
            annotation_ids=[
                event.annotation_id for event in later_favorable + later_adverse
            ],
            research_gaps=(
                trace.warnings
                + ["Review later treatment before assigning argument impact."]
                if not later_favorable and not later_adverse
                else trace.warnings
            ),
        ),
    )

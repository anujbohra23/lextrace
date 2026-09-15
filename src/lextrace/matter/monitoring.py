"""Bounded, evidence-first local monitoring over successive Case corpora."""

import hashlib
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from pydantic import Field, model_validator

from lextrace.domain.case import Case
from lextrace.graph.courts import classify_authority
from lextrace.graph.intelligence import PrecedentIntelligence
from lextrace.graph.intelligence_contracts import DoctrineAnalysis, TreatmentAnnotation
from lextrace.graph.store import CitationGraph
from lextrace.matter.contracts import (
    ArgumentFinding,
    ClaimAuthorityLink,
    CounterAuthority,
    LegalClaim,
    MatterError,
    MatterEvidence,
)
from lextrace.matter.deep_research import assess_coverage
from lextrace.matter.monitoring_contracts import (
    AlertSeverity,
    ChangeEvent,
    ChangeImpact,
    ImpactCategory,
    MatterAlert,
    MonitoringLimits,
    MonitoringPolicy,
    MonitoringRun,
    MonitoringTarget,
)
from lextrace.matter.monitoring_corpus import (
    corpus_delta,
    corpus_version,
    graph_edge_keys,
    graph_incoming_edge_keys,
)
from lextrace.matter.research_contracts import ResearchCoverage
from lextrace.matter.store import MatterStore
from lextrace.retrieval.bm25 import BM25
from lextrace.retrieval.contracts import Diagnostics, Record, RetrievalResult
from lextrace.retrieval.documents import Corpus
from lextrace.retrieval.engine import LexTraceRetriever
from lextrace.retrieval.passages import segment, select
from lextrace.retrieval.settings import PassageSettings


class ImpactDraft(Record):
    """Untrusted generation draft; never accepted as a verified judgment."""

    claim_id: str = Field(min_length=1)
    case_id: str
    passage_id: str
    category: ImpactCategory
    exact_quote: str = Field(min_length=3, max_length=600)
    explanation: str = Field(min_length=3, max_length=1000)
    evidence_ids: list[str] = Field(default_factory=list)


class ImpactJudgment(ImpactDraft):
    """Evidence-bound Stage 2 judgment, still requiring provenance verification."""

    @model_validator(mode="after")
    def require_references(self) -> "ImpactJudgment":
        if self.category not in {"NO_MATERIAL_EFFECT", "INSUFFICIENT_EVIDENCE"}:
            if not self.passage_id or self.passage_id not in self.evidence_ids:
                raise ValueError(
                    "Positive impact requires its passage evidence reference."
                )
        return self


@dataclass(frozen=True)
class MonitoringEvidence:
    run_id: str
    claim: LegalClaim
    finding: ArgumentFinding | None
    result: RetrievalResult
    authority_relationship: str
    treatment: TreatmentAnnotation | None
    doctrine: DoctrineAnalysis | None
    coverage: ResearchCoverage | None
    citation_provenance_ids: list[str]

    def valid_ids(self) -> set[str]:
        identifiers = {self.claim.claim_id, self.result.relevant_passage.passage_id}
        identifiers.update(self.citation_provenance_ids)
        if self.finding is not None:
            identifiers.update(item.evidence_id for item in self.finding.evidence)
            identifiers.update(
                item.result.relevant_passage.passage_id
                for item in self.finding.evidence
            )
        if self.treatment is not None:
            identifiers.add(self.treatment.annotation_id)
        if self.coverage is not None:
            identifiers.update(gap.gap_id for gap in self.coverage.gaps)
        if self.doctrine is not None:
            identifiers.update(event.event_id for event in self.doctrine.events)
            identifiers.update(self.doctrine.state.supporting_annotation_ids)
            identifiers.update(self.doctrine.state.limiting_annotation_ids)
        return identifiers


class ImpactJudge(Protocol):
    def judge(self, evidence: MonitoringEvidence) -> ImpactJudgment: ...


def _stable(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:32]


def _now() -> datetime:
    return datetime.now(UTC)


def _candidate_claims(store: MatterStore, target: MonitoringTarget) -> list[LegalClaim]:
    matter_id = target.matter_id
    if target.target_type in {"CLAIM", "DOCTRINE"}:
        claim = store.claim(matter_id, target.target_reference_id)
        return [claim] if claim is not None and not claim.irrelevant else []
    if target.target_type == "ISSUE":
        return [
            claim
            for claim in store.all_claims(matter_id)
            if claim.issue_id == target.target_reference_id and not claim.irrelevant
        ][:20]
    if target.target_type == "AUTHORITY":
        return [
            claim
            for claim in store.all_claims(matter_id)
            if not claim.irrelevant
            and (finding := store.finding(matter_id, claim.claim_id)) is not None
            and any(
                link.case_id == target.target_reference_id and not link.removed
                for link in finding.cited_authorities
            )
        ][:20]
    return [
        claim
        for claim in store.all_claims(matter_id)
        if not claim.irrelevant
        and (
            claim.importance == "high"
            or store.research_coverage(claim.claim_id) is not None
        )
    ][:20]


def _passage_result(
    case: Case, claim: LegalClaim, score: float
) -> RetrievalResult | None:
    passage = select(
        segment(case, PassageSettings()), claim.normalized_proposition, {}, 1
    )[0]
    if passage.score <= 0:
        return None
    return RetrievalResult(
        case_id=case.source_id,
        rank=1,
        final_score=score,
        retrieval_method="bm25",
        case_name=case.name,
        court=case.court_id,
        date_filed=case.date_filed,
        reporter_citations=case.reporter_citations,
        source_url=case.source_url,
        relevant_passage=passage,
        diagnostics=Diagnostics(bm25_score=score, bm25_rank=1),
    )


def _severity(
    category: ImpactCategory, relationship: str, reviewed_limit: bool
) -> AlertSeverity:
    if reviewed_limit:
        return "HIGH"
    if category in {"WEAKENS", "CREATES_CONFLICT"} and relationship in {
        "CONTROLLING",
        "SAME_COURT",
    }:
        return "HIGH"
    if category in {"WEAKENS", "CREATES_CONFLICT"}:
        return "MEDIUM"
    if category in {"STRENGTHENS", "RESOLVES_GAP"}:
        return "LOW"
    return "INFORMATIONAL"


def verify_change(
    store: MatterStore,
    run: MonitoringRun,
    target: MonitoringTarget,
    case: Case,
    event: ChangeEvent,
    impact: ChangeImpact,
) -> bool:
    """Require current-run source, active target, passage, and versions."""
    claim = store.claim(event.matter_id, impact.claim_id)
    matter = store.get_matter(event.matter_id)
    passage = event.passage
    treatment = event.treatment
    return bool(
        target.enabled
        and target.matter_id == event.matter_id
        and target.target_id == event.target_id
        and claim is not None
        and matter is not None
        and claim.claim_id in event.affected_claim_ids
        and event.monitoring_run_id == run.run_id
        and event.new_case_id == case.source_id == impact.new_case_id
        and case.source_id in run.new_corpus.case_ids
        and (
            run.old_corpus.version != run.new_corpus.version
            or run.old_graph_version != run.new_graph_version
            or treatment is not None
            and treatment.review_state == "CONFIRMED"
            and treatment.reviewed_at is not None
            and target.last_checked_at is not None
            and treatment.reviewed_at > target.last_checked_at
        )
        and passage is not None
        and event.result is not None
        and event.result.case_id == case.source_id
        and event.result.relevant_passage == passage
        and any(
            opinion.source_id == passage.opinion_id
            and opinion.text[passage.start : passage.end] == passage.text
            for opinion in case.opinions
        )
        and impact.passage == passage
        and (
            impact.confidence != "MEDIUM"
            or (
                passage.passage_id in event.judgment_evidence_ids
                and set(event.judgment_evidence_ids)
                <= MonitoringEvidence(
                    run_id=run.run_id,
                    claim=claim,
                    finding=store.finding(event.matter_id, claim.claim_id),
                    result=event.result,
                    authority_relationship=impact.authority_relationship,
                    treatment=treatment,
                    doctrine=store.latest_doctrine(event.matter_id, claim.claim_id),
                    coverage=store.research_coverage(claim.claim_id),
                    citation_provenance_ids=event.citation_provenance_ids,
                ).valid_ids()
            )
        )
        and impact.authority_relationship
        == classify_authority(
            case.source_id,
            case.court_id,
            matter.court,
        ).category
        and (
            treatment is None
            or (
                treatment.graph_id == run.new_graph_version
                and treatment.citing_case_id == case.source_id
                and treatment.cited_case_id in event.affected_case_ids
                and treatment.passage is not None
                and treatment.passage.passage == passage
            )
        )
    )


class MonitorService:
    """One bounded run; a scheduler may call this without changing its logic."""

    def __init__(
        self,
        store: MatterStore,
        old: Corpus,
        new: Corpus,
        *,
        old_graph: CitationGraph | None = None,
        new_graph: CitationGraph | None = None,
        judge: ImpactJudge | None = None,
        limits: MonitoringLimits | None = None,
        old_index_identity: str | None = None,
        new_index_identity: str | None = None,
    ) -> None:
        self.store = store
        self.old = old
        self.new = new
        self.old_graph = old_graph
        self.new_graph = new_graph
        self.judge = judge
        self.limits = limits or MonitoringLimits()
        self.old_index_identity = old_index_identity
        self.new_index_identity = new_index_identity
        self._intelligence = (
            PrecedentIntelligence(LexTraceRetriever(new, graph=new_graph))
            if new_graph is not None
            else None
        )

    def run(
        self, matter_ids: list[str] | None = None, *, run_id: str | None = None
    ) -> MonitoringRun:
        started = time.monotonic()
        old_version = corpus_version(
            self.old,
            index_identity=self.old_index_identity,
            graph_identity=self.old_graph.metadata.graph_id if self.old_graph else None,
        )
        new_version = corpus_version(
            self.new,
            index_identity=self.new_index_identity,
            graph_identity=self.new_graph.metadata.graph_id if self.new_graph else None,
        )
        selected = sorted(
            set(matter_ids or [m.matter_id for m in self.store.list_matters()])
        )
        run = MonitoringRun(
            run_id=run_id or uuid.uuid4().hex,
            status="running",
            started_at=_now(),
            old_corpus=old_version,
            new_corpus=new_version,
            old_graph_version=old_version.graph_identity,
            new_graph_version=new_version.graph_identity,
            matter_ids=selected[: self.limits.max_matters],
        )
        self.store.save_monitoring_run(run)
        case_delta = corpus_delta(old_version, new_version)
        watched: set[str] = set()
        reviewed_case_ids: set[str] = set()
        for matter_id in run.matter_ids:
            if self.store.get_matter(matter_id) is None:
                continue
            self.store.ensure_automatic_monitoring_targets(matter_id)
            for target in self.store.monitoring_targets(matter_id):
                if not target.enabled:
                    continue
                if target.target_type == "AUTHORITY":
                    watched.add(target.target_reference_id)
                for claim in _candidate_claims(self.store, target):
                    finding = self.store.finding(matter_id, claim.claim_id)
                    if finding is not None:
                        watched.update(
                            link.case_id
                            for link in finding.cited_authorities
                            if not link.removed
                        )
                for alert in self.store.matter_alerts(matter_id):
                    event = self.store.change_event(matter_id, alert.event_id)
                    if event is None or event.treatment is None:
                        continue
                    reviewed = self.store.treatment_review(
                        matter_id, alert.claim_id, event.treatment.annotation_id
                    )
                    if (
                        reviewed is not None
                        and reviewed.review_state == "CONFIRMED"
                        and reviewed.reviewed_at is not None
                        and (
                            target.last_checked_at is None
                            or reviewed.reviewed_at > target.last_checked_at
                        )
                    ):
                        reviewed_case_ids.add(alert.new_case_id)
        graph_watch_ids = sorted(watched, key=int)[: self.limits.max_new_cases]
        graph_case_ids = (
            case_delta.added_case_ids
            + case_delta.metadata_changed_case_ids
            + case_delta.text_changed_case_ids
        )[: self.limits.max_new_cases]
        old_edges = graph_edge_keys(self.old_graph, graph_case_ids)
        old_edges |= graph_incoming_edge_keys(self.old_graph, graph_watch_ids)
        new_edges = graph_edge_keys(self.new_graph, graph_case_ids)
        new_edges |= graph_incoming_edge_keys(self.new_graph, graph_watch_ids)
        delta = corpus_delta(
            old_version,
            new_version,
            old_edges=old_edges,
            new_edges=new_edges,
        )
        run.delta = delta
        graph_ids = sorted(
            {
                edge.split(":", 1)[0]
                for edge in delta.citation_edges_added
                if edge.split(":", 1)[0] in self.new.by_id
            }
            | reviewed_case_ids,
            key=int,
        )
        limit_reached = (
            len(selected) > self.limits.max_matters
            or len(delta.added_case_ids) > self.limits.max_new_cases
            or len(watched) > self.limits.max_new_cases
        )
        new_ids = sorted(set(delta.added_case_ids) | set(graph_ids), key=int)[
            : self.limits.max_new_cases
        ]
        run.graph_cases_examined = len(set(graph_ids) - set(delta.added_case_ids))
        if not new_ids:
            if delta.metadata_changed_case_ids or delta.text_changed_case_ids:
                run.errors.append(
                    "Changed source records require explicit reindexing and review; "
                    "they were not treated as new cases."
                )
            for matter_id in run.matter_ids:
                if self.store.get_matter(matter_id) is None:
                    continue
                run.matters_examined += 1
                self.store.ensure_automatic_monitoring_targets(matter_id)
                for target in self.store.monitoring_targets(matter_id):
                    if not target.enabled:
                        continue
                    run.targets_examined += 1
                    target.last_checked_at = _now()
                    target.last_corpus_version = new_version.version
                    target.last_graph_version = new_version.graph_identity
                    self.store.save_monitoring_target(target)
            run.outcome = "LIMIT_REACHED" if limit_reached else "NO_MATERIAL_CHANGE"
            return self._finish(run, started)
        lexical = BM25([self.new.by_id[case_id] for case_id in new_ids])
        examined: set[tuple[str, str]] = set()
        for matter_id in run.matter_ids:
            if time.monotonic() - started >= self.limits.max_runtime_seconds:
                limit_reached = True
                break
            matter = self.store.get_matter(matter_id)
            if matter is None:
                run.errors.append("Matter was not found during monitoring.")
                continue
            run.matters_examined += 1
            policy = self.store.monitoring_policy(matter_id)
            self.store.ensure_automatic_monitoring_targets(matter_id)
            for target in self.store.monitoring_targets(matter_id):
                if not target.enabled or target.target_type not in policy.target_types:
                    continue
                run.targets_examined += 1
                for claim in _candidate_claims(self.store, target):
                    rankings = lexical.rank(
                        claim.claim_id,
                        claim.normalized_proposition,
                        run.run_id,
                    )
                    candidates = [
                        (ranked, self._citation_ids(ranked.case_id, target))
                        for ranked in rankings
                    ]
                    candidates.sort(
                        key=lambda row: (
                            -bool(row[1]),
                            -row[0].score,
                            int(row[0].case_id),
                        )
                    )
                    considered = 0
                    for ranked, citation_ids in candidates:
                        if (
                            time.monotonic() - started
                            >= self.limits.max_runtime_seconds
                        ):
                            limit_reached = True
                            break
                        if considered >= min(
                            policy.max_candidates_per_target,
                            self.limits.max_candidates_per_target,
                        ):
                            break
                        if (claim.claim_id, ranked.case_id) in examined:
                            continue
                        case = self.new.by_id[ranked.case_id]
                        if not self._eligible(case, policy, target):
                            continue
                        normalized_score = ranked.score / (ranked.score + 1)
                        if (
                            not citation_ids
                            and normalized_score < policy.relevance_threshold
                        ):
                            continue
                        examined.add((claim.claim_id, ranked.case_id))
                        considered += 1
                        run.stage_one_candidates += 1
                        if run.stage_two_analyses >= self.limits.max_stage_two:
                            limit_reached = True
                            break
                        run.stage_two_analyses += 1
                        self._analyze(
                            run,
                            matter_id,
                            target,
                            claim,
                            case,
                            ranked.score,
                            citation_ids,
                        )
                        if (
                            self.limits.max_tokens > 0
                            and run.input_tokens + run.output_tokens
                            >= self.limits.max_tokens
                        ):
                            limit_reached = True
                            break
                    if limit_reached:
                        break
                target.last_checked_at = _now()
                target.last_corpus_version = new_version.version
                target.last_graph_version = new_version.graph_identity
                target.updated_at = _now()
                self.store.save_monitoring_target(target)
                if limit_reached:
                    break
            if limit_reached:
                break
        run.new_cases_examined = len(delta.added_case_ids[: self.limits.max_new_cases])
        if limit_reached:
            run.outcome = "LIMIT_REACHED"
            run.errors.append(
                "LIMIT_REACHED: Monitoring stopped at a configured bound."
            )
        elif run.alerts_created:
            run.outcome = "ALERTS"
        else:
            run.outcome = "NO_MATERIAL_CHANGE"
        return self._finish(run, started)

    def _finish(self, run: MonitoringRun, started: float) -> MonitoringRun:
        run.status = "completed"
        run.completed_at = _now()
        run.elapsed_ms = round((time.monotonic() - started) * 1000)
        self.store.save_monitoring_run(run)
        return run

    def _eligible(
        self, case: Case, policy: MonitoringPolicy, target: MonitoringTarget
    ) -> bool:
        scope = target.monitoring_scope
        return bool(
            (not policy.courts or case.court_id in policy.courts)
            and (not scope.courts or case.court_id in scope.courts)
            and (
                policy.filed_after is None
                or case.date_filed is not None
                and case.date_filed >= policy.filed_after
            )
            and (
                policy.filed_before is None
                or case.date_filed is not None
                and case.date_filed <= policy.filed_before
            )
            and (
                target.as_of_date is None
                or case.date_filed is not None
                and case.date_filed > target.as_of_date
            )
        )

    def _citation_ids(self, case_id: str, target: MonitoringTarget) -> list[str]:
        if self.new_graph is None:
            return []
        monitored = (
            {target.target_reference_id} if target.target_type == "AUTHORITY" else set()
        )
        for claim in _candidate_claims(self.store, target):
            finding = self.store.finding(target.matter_id, claim.claim_id)
            if finding is not None:
                monitored.update(
                    link.case_id
                    for link in finding.cited_authorities
                    if not link.removed
                )
        return sorted(
            {
                support.provenance_id
                for neighbor in self.new_graph.get_outgoing(case_id, limit=100)
                if neighbor.edge.cited_case_id in monitored
                for support in neighbor.edge.supports
            }
        )

    def _observed_treatment(
        self, matter_id: str, claim: LegalClaim, new_case_id: str
    ) -> TreatmentAnnotation | None:
        if self._intelligence is None or self.new_graph is None:
            return None
        finding = self.store.finding(matter_id, claim.claim_id)
        if finding is None:
            return None
        monitored = {
            link.case_id for link in finding.cited_authorities if not link.removed
        }
        for neighbor in self.new_graph.get_outgoing(new_case_id, limit=100):
            cited = neighbor.edge.cited_case_id
            if cited not in monitored:
                continue
            trace = self._intelligence.trace(
                cited,
                proposition=claim.normalized_proposition,
                backward_depth=0,
                forward_depth=1,
                max_nodes=10,
                max_edges=20,
            )
            for edge in trace.edges:
                annotation = edge.treatment
                if edge.edge.citing_case_id != new_case_id:
                    continue
                reviewed = self.store.treatment_review(
                    matter_id, claim.claim_id, annotation.annotation_id
                )
                if (
                    reviewed is not None
                    and reviewed.review_state == "CONFIRMED"
                    and reviewed.passage == annotation.passage
                    and reviewed.generated_label == annotation.generated_label
                ):
                    return reviewed
                if reviewed is None or reviewed.review_state != "REJECTED":
                    return annotation
        return None

    def _analyze(
        self,
        run: MonitoringRun,
        matter_id: str,
        target: MonitoringTarget,
        claim: LegalClaim,
        case: Case,
        score: float,
        citations: list[str],
    ) -> None:
        result = _passage_result(case, claim, score)
        if result is None:
            return
        matter = self.store.get_matter(matter_id)
        if matter is None:
            return
        relationship = classify_authority(case.source_id, case.court_id, matter.court)
        treatment = self._observed_treatment(matter_id, claim, case.source_id)
        if treatment is not None and treatment.passage is not None:
            result = result.model_copy(
                update={"relevant_passage": treatment.passage.passage}
            )
        finding = self.store.finding(matter_id, claim.claim_id)
        coverage = self.store.research_coverage(claim.claim_id)
        doctrine = self.store.latest_doctrine(matter_id, claim.claim_id)
        evidence = MonitoringEvidence(
            run_id=run.run_id,
            claim=claim,
            finding=finding,
            result=result,
            authority_relationship=relationship.category,
            treatment=treatment,
            doctrine=doctrine,
            coverage=coverage,
            citation_provenance_ids=citations,
        )
        category: ImpactCategory = "NEW_RELEVANT_AUTHORITY"
        explanation = (
            "A new case contains a matching passage; legal effect needs review."
        )
        confidence: Literal["HIGH", "MEDIUM", "LOW"] = "LOW"
        judgment: ImpactJudgment | None = None
        usage = getattr(self.judge, "usage", None)
        if (
            self.judge is not None
            and run.llm_calls + getattr(self.judge, "max_calls", 1)
            <= self.limits.max_llm_calls
            and (
                usage is None
                or self.limits.max_tokens > run.input_tokens + run.output_tokens
            )
        ):
            run.llm_calls += 1
            prior_input = usage.input_tokens if usage is not None else 0
            prior_output = usage.output_tokens if usage is not None else 0
            prior_calls = usage.calls if usage is not None else 0
            try:
                candidate = self.judge.judge(evidence)
                if (
                    candidate.claim_id == claim.claim_id
                    and candidate.case_id == case.source_id
                    and candidate.passage_id == result.relevant_passage.passage_id
                    and candidate.exact_quote in result.relevant_passage.text
                    and (
                        candidate.category
                        in {"NO_MATERIAL_EFFECT", "INSUFFICIENT_EVIDENCE"}
                        or candidate.evidence_ids
                        and result.relevant_passage.passage_id in candidate.evidence_ids
                    )
                    and set(candidate.evidence_ids) <= evidence.valid_ids()
                    and not (
                        treatment is not None
                        and treatment.review_state != "CONFIRMED"
                        and treatment.annotation_id in candidate.evidence_ids
                        and candidate.category
                        in {
                            "STRENGTHENS",
                            "WEAKENS",
                            "CREATES_CONFLICT",
                            "RESOLVES_GAP",
                        }
                    )
                    and (
                        candidate.category != "RESOLVES_GAP"
                        or coverage is not None
                        and any(
                            gap.gap_id in candidate.evidence_ids and not gap.resolved
                            for gap in coverage.gaps
                        )
                    )
                ):
                    judgment = candidate
                else:
                    run.rejected_impacts += 1
            except Exception:
                run.rejected_impacts += 1
                run.errors.append("Structured impact judgment failed safely.")
            finally:
                if usage is not None:
                    run.llm_calls += max(0, usage.calls - prior_calls - 1)
                    run.input_tokens += usage.input_tokens - prior_input
                    run.output_tokens += usage.output_tokens - prior_output
        if judgment is not None:
            if judgment.category in {"NO_MATERIAL_EFFECT", "INSUFFICIENT_EVIDENCE"}:
                return
            category = judgment.category
            explanation = judgment.explanation
            confidence = "MEDIUM" if category != "NEW_RELEVANT_AUTHORITY" else "LOW"
        elif citations and score <= 0:
            return
        if treatment is not None and treatment.review_state == "CONFIRMED":
            if treatment.generated_label in {"LIMITS", "OVERRULES", "DISTINGUISHES"}:
                category = "WEAKENS"
                explanation = (
                    "Human-confirmed citation context limits or distinguishes "
                    "an authority used by this claim."
                )
                confidence = "HIGH"
            elif treatment.generated_label in {"FOLLOWS", "APPLIES"}:
                category = "STRENGTHENS"
                explanation = (
                    "Human-confirmed citation context follows a linked authority."
                )
                confidence = "HIGH"
        controlling = relationship.category in {"CONTROLLING", "SAME_COURT"}
        if (
            category == "STRENGTHENS"
            and controlling
            and coverage is not None
            and any(
                gap.type == "NO_CONTROLLING_AUTHORITY" and not gap.resolved
                for gap in coverage.gaps
            )
        ):
            category = "RESOLVES_GAP"
            explanation = (
                "New verified controlling support may resolve a recorded research gap."
            )
        event_type = (
            "RESEARCH_GAP_RESOLVED"
            if category == "RESOLVES_GAP"
            else "NEW_TREATMENT"
            if treatment is not None
            else "NEW_COUNTER_AUTHORITY"
            if category in {"WEAKENS", "CREATES_CONFLICT"}
            else "NEW_CITING_AUTHORITY"
            if citations
            else "NEW_CONTROLLING_AUTHORITY"
            if controlling
            else "NEW_SUPPORTING_AUTHORITY"
            if category == "STRENGTHENS"
            else "NEW_RELEVANT_AUTHORITY"
        )
        policy = self.store.monitoring_policy(matter_id)
        if (
            event_type not in policy.enabled_event_types
            or event_type not in target.monitoring_scope.enabled_event_types
        ):
            return
        event = ChangeEvent(
            event_id=_stable(matter_id, claim.claim_id, case.source_id, event_type),
            monitoring_run_id=run.run_id,
            matter_id=matter_id,
            target_id=target.target_id,
            event_type=event_type,
            new_case_id=case.source_id,
            affected_claim_ids=[claim.claim_id],
            affected_case_ids=sorted(
                {link.case_id for link in finding.cited_authorities if not link.removed}
            )
            if finding is not None
            else [],
            passage=result.relevant_passage,
            result=result,
            citation_provenance_ids=citations,
            judgment_evidence_ids=judgment.evidence_ids if judgment else [],
            treatment=treatment,
            discovered_at=_now(),
            verification_state="VERIFIED",
        )
        impact = ChangeImpact(
            impact_id=_stable(
                event.event_id, category, result.relevant_passage.passage_id
            ),
            event_id=event.event_id,
            matter_id=matter_id,
            claim_id=claim.claim_id,
            previous_finding_status=finding.vulnerability if finding else None,
            previous_coverage=coverage.category if coverage else None,
            previous_doctrine=doctrine.impact.category if doctrine else None,
            new_doctrine=None,
            category=category,
            explanation=explanation,
            affected_proposition=claim.normalized_proposition,
            new_case_id=case.source_id,
            passage=result.relevant_passage,
            authority_relationship=relationship.category,
            treatment_relationship=(
                treatment.generated_label
                if treatment is not None and treatment.review_state == "CONFIRMED"
                else None
            ),
            confidence=confidence,
            created_at=_now(),
        )
        if not verify_change(self.store, run, target, case, event, impact):
            run.rejected_impacts += 1
            return
        self.store.save_change_event(event)
        self.store.save_change_impact(impact)
        run.events_created += 1
        run.verified_impacts += 1
        if category in {"NO_MATERIAL_EFFECT", "INSUFFICIENT_EVIDENCE"}:
            return
        severity = _severity(
            category,
            relationship.category,
            treatment is not None
            and treatment.review_state == "CONFIRMED"
            and treatment.generated_label in {"LIMITS", "OVERRULES"},
        )
        alert = MatterAlert(
            alert_id=_stable(matter_id, claim.claim_id, case.source_id, event_type),
            matter_id=matter_id,
            claim_id=claim.claim_id,
            event_id=event.event_id,
            impact_id=impact.impact_id,
            new_case_id=case.source_id,
            severity=severity,
            title=f"New authority may affect a monitored claim ({case.name}).",
            explanation=explanation,
            created_at=_now(),
            updated_at=_now(),
        )
        previous = self.store.matter_alert(matter_id, alert.alert_id)
        self.store.save_matter_alert(alert)
        if previous is None:
            run.alerts_created += 1


def apply_alert(store: MatterStore, matter_id: str, alert_id: str) -> MatterAlert:
    alert = store.matter_alert(matter_id, alert_id)
    if alert is None:
        raise MatterError("Matter alert was not found.")
    if alert.review_state == "APPLIED":
        return alert
    if alert.review_state in {"DISMISSED", "NOT_RELEVANT"}:
        raise MatterError("Dismissed or irrelevant alert cannot be applied.")
    event = store.change_event(matter_id, alert.event_id)
    impact = store.change_impact(matter_id, alert.impact_id)
    claim = store.claim(matter_id, alert.claim_id)
    finding = store.finding(matter_id, alert.claim_id)
    if (
        event is None
        or impact is None
        or claim is None
        or finding is None
        or event.verification_state != "VERIFIED"
        or event.result is None
        or event.passage != event.result.relevant_passage
    ):
        raise MatterError("Alert evidence is unavailable for application.")
    if impact.category not in {
        "STRENGTHENS",
        "RESOLVES_GAP",
        "WEAKENS",
        "CREATES_CONFLICT",
    }:
        raise MatterError("Review the legal effect before applying this alert.")
    is_counter = impact.category in {"WEAKENS", "CREATES_CONFLICT"}
    evidence_id = _stable(alert.alert_id, "applied-evidence")
    if not any(item.evidence_id == evidence_id for item in finding.evidence):
        evidence = MatterEvidence(
            evidence_id=evidence_id,
            claim_id=claim.claim_id,
            role="counter" if is_counter else "independent_support",
            retrieval_query=claim.normalized_proposition,
            result=event.result,
        )
        finding.evidence.append(evidence)
        if is_counter:
            finding.counter_authorities.append(
                CounterAuthority(
                    claim_id=claim.claim_id,
                    evidence=evidence,
                    research_query=claim.normalized_proposition,
                )
            )
        finding.cited_authorities.append(
            ClaimAuthorityLink(
                claim_id=claim.claim_id,
                case_id=event.new_case_id,
                relation="COUNTERS" if is_counter else "SUPPORTS",
                evidence_id=evidence_id,
            )
        )
        # New evidence invalidates the old assessment; it does not establish a
        # replacement legal conclusion without normal claim reanalysis.
        claim.verification_state = "INSUFFICIENT_EVIDENCE"
        finding.verification_status = "INSUFFICIENT_EVIDENCE"
        finding.vulnerability = "INSUFFICIENT_EVIDENCE"
        finding.explanation = (
            "Monitoring evidence was applied; reanalyze this claim before relying "
            "on its previous assessment."
        )
        store.update_claim(claim)
        store.update_finding(finding)
        matter = store.get_matter(matter_id)
        if matter is None:
            raise MatterError("Matter was not found.")
        store.save_research_coverage(assess_coverage(matter, claim, finding))
    impact.review_state = "APPLIED"
    store.save_change_impact(impact)
    return store.review_matter_alert(matter_id, alert_id, "APPLIED")

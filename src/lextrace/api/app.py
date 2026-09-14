"""Production-style local API with bounded background legal research."""

import hashlib
import json
import threading
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Literal, cast

from fastapi import (
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from pydantic import Field

from lextrace.config import (
    APP_TITLE,
    AppSettings,
    ConfigurationError,
    openai_api_key,
)
from lextrace.domain.case import Case
from lextrace.graph.contracts import CitationNeighbor, GraphError
from lextrace.graph.courts import classify_authority
from lextrace.graph.intelligence import (
    TRACE_VERSION,
    TREATMENT_VERSION,
    PrecedentIntelligence,
    doctrine_for_claim,
)
from lextrace.graph.intelligence_contracts import (
    ClaimAuthorityAnalysis,
    DoctrineAnalysis,
    PrecedentTrace,
    TreatmentAnnotation,
)
from lextrace.matter.analysis import MatterAnalyzer
from lextrace.matter.contracts import ArgumentFinding, LegalClaim, Matter, MatterError
from lextrace.matter.deep_research import (
    assess_coverage,
    claim_fingerprint,
    propose_plan,
)
from lextrace.matter.documents import MAX_UPLOAD_BYTES
from lextrace.matter.matrix import filter_matrix, matrix_csv, matrix_rows
from lextrace.matter.red_team import (
    attack_surface,
)
from lextrace.matter.research_contracts import (
    AttackFinding,
    AttackSurface,
    DeepResearchPlan,
    DeepResearchRun,
    EvidenceMatrixRow,
    ResearchCoverage,
    ResearchStep,
)
from lextrace.matter.store import MatterStore
from lextrace.matter.workflows import (
    DeepResearchState,
    RedTeamState,
    deep_research_graph,
    red_team_graph,
)
from lextrace.research.contracts import ResearchError, ResearchRequest
from lextrace.research.llm import OpenAICompatibleLLM
from lextrace.research.runtime import (
    CachedStructuredLLM,
    ResearchJobs,
    RunStore,
    StructuredCache,
)
from lextrace.research.workflow import ResearchWorkflow
from lextrace.retrieval.contracts import (
    Record,
    RetrievalError,
    SearchRequest,
    SearchResponse,
)
from lextrace.retrieval.engine import LexTraceRetriever


class RunAccepted(Record):
    run_id: str
    status: Literal["queued"] = "queued"


class MatterCreate(Record):
    name: str
    court: str | None = None
    jurisdiction: str | None = None
    state: str | None = None
    as_of_date: date | None = None
    reference: str | None = None


class ClaimEdit(Record):
    normalized_proposition: str | None = None
    irrelevant: bool | None = None
    wording_locked: bool | None = None


class PlanEdit(Record):
    steps: list[ResearchStep] | None = None
    approve: bool = False
    max_rounds: int | None = Field(default=None, ge=1, le=3)
    max_queries_per_round: int | None = Field(default=None, ge=1, le=6)
    max_total_results: int | None = Field(default=None, ge=1, le=60)
    max_duration_seconds: int | None = Field(default=None, ge=1, le=300)


class AuthorityEdit(Record):
    pinned: bool | None = None
    removed: bool | None = None
    evidence_id: str | None = None


class TreatmentReview(Record):
    state: Literal["CONFIRMED", "REJECTED", "UNCERTAIN"]


def create_app(
    retriever: LexTraceRetriever | None = None,
    *,
    index_path: Path | None = None,
    graph_path: Path | None = None,
    research_jobs: ResearchJobs | None = None,
    settings: AppSettings | None = None,
) -> FastAPI:
    configured = settings or AppSettings.from_environment()
    lock = threading.RLock()
    engine = retriever
    jobs = research_jobs
    owned_store: RunStore | None = None
    owned_cache: StructuredCache | None = None
    matter_store: MatterStore | None = None
    matter_executor = ThreadPoolExecutor(max_workers=1)
    matter_slots = threading.BoundedSemaphore(3)

    def get_matter_store() -> MatterStore:
        nonlocal matter_store
        with lock:
            if matter_store is None:
                matter_store = MatterStore(
                    configured.matter_db, configured.private_matter_root
                )
        return matter_store

    def get_matter_analyzer(matter_id: str) -> tuple[MatterAnalyzer, StructuredCache]:
        if not configured.llm_model:
            raise MatterError("LLM model is not configured.")
        try:
            provider = OpenAICompatibleLLM(
                configured.llm_model,
                api_key=openai_api_key(),
                base_url=configured.llm_base_url,
            )
        except (ResearchError, ConfigurationError):
            raise MatterError("Matter analysis provider is not configured.") from None
        cache = StructuredCache(
            configured.private_matter_root / matter_id / "structured-cache.sqlite3"
        )
        return MatterAnalyzer(get_engine(), CachedStructuredLLM(provider, cache)), cache

    def reanalyze_one_claim(
        store: MatterStore,
        analyzer: MatterAnalyzer,
        matter_id: str,
        claim_id: str,
    ) -> None:
        claim = store.claim(matter_id, claim_id)
        if claim is None:
            raise MatterError("Claim was not found.")
        citations = store.analysis(matter_id, claim.document_id).citations
        previous = store.finding(matter_id, claim_id)
        finding = analyzer.analyze_claim(claim, citations)
        if previous is not None:
            reviews = {link.evidence_id: link for link in previous.cited_authorities}
            for link in finding.cited_authorities:
                reviewed = reviews.get(link.evidence_id)
                if reviewed is not None:
                    link.pinned = reviewed.pinned
                    link.removed = reviewed.removed
            if any(link.removed for link in finding.cited_authorities):
                finding.verification_status = "INSUFFICIENT_EVIDENCE"
                finding.vulnerability = "INSUFFICIENT_EVIDENCE"
                finding.explanation = (
                    "A reviewer removed an authority; recheck coverage."
                )
        claim.verification_state = finding.verification_status
        store.update_claim(claim)
        store.update_finding(finding)

    def submit_matter_analysis(
        matter_id: str, document_id: str, claim_id: str | None = None
    ) -> str:
        store = get_matter_store()
        document = store.get_document(matter_id, document_id)
        if document is None:
            raise MatterError("Document was not found.")
        if document.ingestion_status != "READY":
            raise MatterError("Document requires extractable text before analysis.")
        if claim_id is not None and store.claim(matter_id, claim_id) is None:
            raise MatterError("Claim was not found.")
        if not matter_slots.acquire(blocking=False):
            raise MatterError("Matter analysis queue is full.")
        try:
            job_id = store.create_job(matter_id, document_id, claim_id)
        except Exception:
            matter_slots.release()
            raise

        def run() -> None:
            private_cache: StructuredCache | None = None
            try:
                store.set_job(job_id, "running")
                analyzer, private_cache = get_matter_analyzer(matter_id)
                if claim_id is None:
                    result = analyzer.analyze(
                        matter_id,
                        document_id,
                        store.document_text(matter_id, document_id),
                        store.sections(matter_id, document_id),
                    )
                    store.save_analysis(result)
                else:
                    reanalyze_one_claim(store, analyzer, matter_id, claim_id)
                store.set_job(job_id, "completed")
            except MatterError as error:
                code = (
                    "MODEL_NOT_CONFIGURED"
                    if str(error) == "LLM model is not configured."
                    else "PROVIDER_NOT_CONFIGURED"
                    if str(error) == "Matter analysis provider is not configured."
                    else "MATTER_INPUT_INVALID"
                )
                store.set_job(job_id, "failed", code)
            except HTTPException:
                store.set_job(job_id, "failed", "RETRIEVAL_UNAVAILABLE")
            except ResearchError:
                store.set_job(job_id, "failed", "PROVIDER_FAILED")
            except RetrievalError:
                store.set_job(job_id, "failed", "RETRIEVAL_FAILED")
            except Exception:
                store.set_job(job_id, "failed", "MATTER_ANALYSIS_FAILED")
            finally:
                if private_cache is not None:
                    private_cache.close()
                matter_slots.release()

        try:
            matter_executor.submit(run)
        except RuntimeError:
            store.set_job(job_id, "failed", "MATTER_QUEUE_UNAVAILABLE")
            matter_slots.release()
            raise MatterError("Matter analysis queue is unavailable.") from None
        return job_id

    def get_engine() -> LexTraceRetriever:
        nonlocal engine
        with lock:
            if engine is None:
                try:
                    engine = LexTraceRetriever.from_index(
                        index_path or configured.index_path,
                        graph_path=graph_path or configured.graph_path,
                    )
                except (RetrievalError, GraphError) as error:
                    raise HTTPException(status_code=503, detail=str(error)) from None
        return engine

    def get_jobs() -> ResearchJobs:
        nonlocal jobs, owned_store, owned_cache
        with lock:
            if jobs is None:
                if not configured.llm_model:
                    raise HTTPException(
                        status_code=503, detail="LLM model is not configured."
                    )
                try:
                    provider = OpenAICompatibleLLM(
                        configured.llm_model,
                        api_key=openai_api_key(),
                        base_url=configured.llm_base_url,
                    )
                except (ResearchError, ConfigurationError) as error:
                    raise HTTPException(status_code=503, detail=str(error)) from None
                owned_store = RunStore(
                    configured.runtime_db,
                    persist_content=configured.persist_content,
                )
                owned_cache = StructuredCache(configured.cache_db)
                jobs = ResearchJobs(
                    ResearchWorkflow(
                        get_engine(), CachedStructuredLLM(provider, owned_cache)
                    ),
                    owned_store,
                    max_workers=configured.max_concurrent_research,
                )
        return jobs

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        yield
        if jobs is not None and jobs is not research_jobs:
            jobs.close()
        if owned_store is not None:
            owned_store.close()
        if owned_cache is not None:
            owned_cache.close()
        if engine is not None:
            engine.close()
        matter_executor.shutdown(wait=True)
        if matter_store is not None:
            matter_store.close()

    application = FastAPI(title=APP_TITLE, lifespan=lifespan)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=configured.cors_origins,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["content-type", "x-request-id"],
    )

    @application.exception_handler(MatterError)
    async def matter_error(_request: Request, error: MatterError) -> Response:
        from fastapi.responses import JSONResponse

        detail = str(error)
        code = (
            404
            if "not found" in detail.lower()
            else 429
            if "queue is full" in detail
            else 400
        )
        return JSONResponse(status_code=code, content={"detail": detail})

    @application.middleware("http")
    async def request_identity(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        return response

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/ready")
    def ready() -> dict[str, str]:
        try:
            get_engine()
        except HTTPException as error:
            raise HTTPException(
                status_code=503, detail="Retrieval engine unavailable."
            ) from error
        return {"status": "ready"}

    @application.post("/matters", status_code=201)
    def create_matter(request: MatterCreate) -> dict[str, object]:
        return (
            get_matter_store()
            .create_matter(**request.model_dump())
            .model_dump(mode="json")
        )

    @application.get("/matters")
    def list_matters() -> list[dict[str, object]]:
        return [m.model_dump(mode="json") for m in get_matter_store().list_matters()]

    @application.get("/matters/{matter_id}")
    def read_matter(matter_id: str) -> dict[str, object]:
        matter = get_matter_store().get_matter(matter_id)
        if matter is None:
            raise MatterError("Matter was not found.")
        return matter.model_dump(mode="json")

    @application.delete("/matters/{matter_id}", status_code=204)
    def delete_matter(matter_id: str) -> None:
        if not get_matter_store().delete_matter(matter_id):
            raise MatterError("Matter was not found.")

    @application.post("/matters/{matter_id}/documents", status_code=201)
    async def upload_document(
        matter_id: str, file: Annotated[UploadFile, File()]
    ) -> dict[str, object]:
        content = await file.read(MAX_UPLOAD_BYTES + 1)
        document = get_matter_store().add_document(
            matter_id, file.filename or "", content
        )
        return document.model_dump(mode="json")

    @application.get("/matters/{matter_id}/documents")
    def list_documents(matter_id: str) -> list[dict[str, object]]:
        return [
            d.model_dump(mode="json")
            for d in get_matter_store().list_documents(matter_id)
        ]

    @application.get("/matters/{matter_id}/documents/{document_id}")
    def read_document(matter_id: str, document_id: str) -> dict[str, object]:
        store = get_matter_store()
        document = store.get_document(matter_id, document_id)
        if document is None:
            raise MatterError("Document was not found.")
        return {
            "document": document.model_dump(mode="json"),
            "text": store.document_text(matter_id, document_id),
            "sections": [
                s.model_dump(mode="json")
                for s in store.sections(matter_id, document_id)
            ],
        }

    @application.post(
        "/matters/{matter_id}/documents/{document_id}/analyze", status_code=202
    )
    def analyze_document(matter_id: str, document_id: str) -> dict[str, str]:
        return {
            "job_id": submit_matter_analysis(matter_id, document_id),
            "status": "queued",
        }

    @application.get("/matters/{matter_id}/jobs/{job_id}")
    def matter_job(matter_id: str, job_id: str) -> dict[str, str | None]:
        row = get_matter_store().job(matter_id, job_id)
        if row is None:
            raise MatterError("Analysis job was not found.")
        return row

    @application.get("/matters/{matter_id}/claims")
    def matter_claims(matter_id: str) -> list[dict[str, object]]:
        return [
            c.model_dump(mode="json") for c in get_matter_store().all_claims(matter_id)
        ]

    @application.get("/matters/{matter_id}/issues")
    def matter_issues(matter_id: str) -> list[dict[str, object]]:
        return [
            i.model_dump(mode="json") for i in get_matter_store().all_issues(matter_id)
        ]

    @application.get("/matters/{matter_id}/claims/{claim_id}")
    def matter_claim(matter_id: str, claim_id: str) -> dict[str, object]:
        store = get_matter_store()
        claim = store.claim(matter_id, claim_id)
        if claim is None:
            raise MatterError("Claim was not found.")
        finding = store.finding(matter_id, claim_id)
        return {
            "claim": claim.model_dump(mode="json"),
            "finding": finding.model_dump(mode="json") if finding else None,
        }

    @application.patch("/matters/{matter_id}/claims/{claim_id}")
    def edit_claim(
        matter_id: str, claim_id: str, request: ClaimEdit
    ) -> dict[str, object]:
        store = get_matter_store()
        claim = store.claim(matter_id, claim_id)
        if claim is None:
            raise MatterError("Claim was not found.")
        if (
            request.wording_locked is not None
            and request.normalized_proposition is None
            and request.irrelevant is None
        ):
            return store.set_claim_lock(
                matter_id, claim_id, request.wording_locked
            ).model_dump(mode="json")
        if request.normalized_proposition is not None:
            if claim.wording_locked:
                raise MatterError("Claim wording is locked. Unlock it before editing.")
            proposition = request.normalized_proposition.strip()
            if not proposition or len(proposition) > 1000:
                raise MatterError("Normalized proposition is invalid.")
            claim.normalized_proposition = proposition
            claim.manually_edited = True
        if request.irrelevant is not None:
            claim.irrelevant = request.irrelevant
        if request.wording_locked is not None:
            claim.wording_locked = request.wording_locked
        claim.verification_state = "INSUFFICIENT_EVIDENCE"
        store.update_claim(claim)
        return claim.model_dump(mode="json")

    @application.post(
        "/matters/{matter_id}/claims/{claim_id}/reanalyze", status_code=202
    )
    def reanalyze_claim(matter_id: str, claim_id: str) -> dict[str, str]:
        claim = get_matter_store().claim(matter_id, claim_id)
        if claim is None:
            raise MatterError("Claim was not found.")
        return {
            "job_id": submit_matter_analysis(matter_id, claim.document_id, claim_id),
            "status": "queued",
        }

    @application.post(
        "/matters/{matter_id}/issues/{issue_id}/reanalyze", status_code=202
    )
    def reanalyze_issue(matter_id: str, issue_id: str) -> dict[str, str]:
        store = get_matter_store()
        if not any(issue.issue_id == issue_id for issue in store.all_issues(matter_id)):
            raise MatterError("Issue was not found.")
        claims = [
            claim for claim in store.all_claims(matter_id) if claim.issue_id == issue_id
        ]
        if not claims:
            raise MatterError("Issue has no claims to analyze.")
        if len(claims) > 8:
            raise MatterError("Issue exceeds the eight-claim rerun limit.")
        if not matter_slots.acquire(blocking=False):
            raise MatterError("Matter analysis queue is full.")
        try:
            job_id = store.create_job(matter_id, claims[0].document_id)

            def perform() -> None:
                private_cache: StructuredCache | None = None
                try:
                    store.set_job(job_id, "running")
                    analyzer, private_cache = get_matter_analyzer(matter_id)
                    for claim in claims:
                        reanalyze_one_claim(store, analyzer, matter_id, claim.claim_id)
                    store.set_job(job_id, "completed")
                except Exception:
                    store.set_job(job_id, "failed", "ISSUE_REANALYSIS_FAILED")
                finally:
                    if private_cache is not None:
                        private_cache.close()
                    matter_slots.release()

            matter_executor.submit(perform)
        except Exception:
            matter_slots.release()
            raise
        return {"job_id": job_id, "status": "queued"}

    @application.patch("/matters/{matter_id}/claims/{claim_id}/authorities/{case_id}")
    def edit_authority(
        matter_id: str, claim_id: str, case_id: str, request: AuthorityEdit
    ) -> dict[str, object]:
        store = get_matter_store()
        finding = store.finding(matter_id, claim_id)
        if finding is None:
            raise MatterError("Claim finding was not found.")
        link = next(
            (
                item
                for item in finding.cited_authorities
                if item.case_id == case_id
                and (
                    request.evidence_id is None
                    or item.evidence_id == request.evidence_id
                )
            ),
            None,
        )
        if link is None:
            raise MatterError("Authority link was not found.")
        if request.pinned is not None:
            link.pinned = request.pinned
        if request.removed is not None:
            link.removed = request.removed
            finding.verification_status = "INSUFFICIENT_EVIDENCE"
            finding.vulnerability = "INSUFFICIENT_EVIDENCE"
            finding.explanation = "Authority review changed; re-analysis is required."
            finding.warnings.append("Authority review changed")
            claim = store.claim(matter_id, claim_id)
            if claim is not None:
                claim.verification_state = "INSUFFICIENT_EVIDENCE"
                store.update_claim(claim)
        store.update_finding(finding)
        return link.model_dump(mode="json")

    @application.get("/matters/{matter_id}/argument-xray")
    def argument_xray(matter_id: str) -> list[dict[str, object]]:
        store = get_matter_store()
        if store.get_matter(matter_id) is None:
            raise MatterError("Matter was not found.")
        return [
            f.model_dump(mode="json")
            for c in store.all_claims(matter_id)
            if (f := store.finding(matter_id, c.claim_id)) is not None
            and not c.irrelevant
        ]

    @application.post("/search", response_model=SearchResponse)
    def search(request: SearchRequest) -> SearchResponse:
        try:
            return get_engine().search_response(request)
        except RetrievalError as error:
            raise HTTPException(status_code=503, detail=str(error)) from None

    @application.post(
        "/research", response_model=RunAccepted, status_code=status.HTTP_202_ACCEPTED
    )
    def research(request: ResearchRequest) -> RunAccepted:
        return RunAccepted(run_id=get_jobs().submit(request))

    @application.get("/research/{run_id}")
    def research_result(run_id: str) -> dict[str, object]:
        current = get_jobs()
        try:
            row = current.store.get(run_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Research run not found.")
            result = current.store.response(run_id)
            return {
                "run_id": run_id,
                "status": row["status"],
                "result": result.model_dump(mode="json") if result else None,
            }
        except ResearchError as error:
            raise HTTPException(status_code=400, detail=str(error)) from None

    @application.get("/research/{run_id}/trace")
    def research_trace(run_id: str) -> dict[str, object]:
        current = get_jobs()
        try:
            trace = current.store.trace(run_id)
            row = current.store.get(run_id)
        except ResearchError as error:
            raise HTTPException(status_code=400, detail=str(error)) from None
        if row is None:
            raise HTTPException(status_code=404, detail="Research run not found.")
        return trace or {"run_id": run_id, "status": row["status"]}

    @application.get("/runs")
    def runs(limit: int = Query(default=20, ge=1, le=100)) -> list[dict[str, object]]:
        return get_jobs().store.list(limit)

    @application.post("/research/{run_id}/resume", response_model=RunAccepted)
    def resume(run_id: str) -> RunAccepted:
        try:
            return RunAccepted(run_id=get_jobs().resume(run_id))
        except ResearchError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None

    @application.get("/cases/{case_id}", response_model=Case)
    def get_case(case_id: str) -> Case:
        case = get_engine().corpus.by_id.get(case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="Case not found.")
        return case

    @application.get(
        "/cases/{case_id}/citations", response_model=list[CitationNeighbor]
    )
    def citations(
        case_id: str,
        direction: Literal["outgoing", "incoming", "both"] = "outgoing",
        limit: int = Query(default=100, ge=1, le=1000),
        as_of_date: date | None = None,
    ) -> list[CitationNeighbor]:
        current = get_engine()
        if current.graph is None:
            raise HTTPException(status_code=503, detail="Citation graph unavailable.")
        if current.graph.get_node(case_id) is None:
            raise HTTPException(status_code=404, detail="Case not found in graph.")
        try:
            return current.graph.neighbors(
                case_id,
                direction=direction,
                limit=limit,
                as_of_date=as_of_date,
            )
        except GraphError as error:
            raise HTTPException(status_code=503, detail=str(error)) from None

    def claim_context(
        matter_id: str, claim_id: str
    ) -> tuple[Matter, LegalClaim, ArgumentFinding]:
        store = get_matter_store()
        matter = store.get_matter(matter_id)
        claim = store.claim(matter_id, claim_id)
        if matter is None or claim is None:
            raise MatterError("Claim was not found.")
        finding = store.finding(matter_id, claim_id)
        if finding is None:
            raise MatterError("Claim requires Argument X-Ray analysis first.")
        return matter, claim, finding

    def trace_for_claim(
        matter_id: str,
        claim_id: str,
        seed_case_id: str | None,
        as_of_date: date | None,
        backward_depth: int,
        forward_depth: int,
        max_nodes: int,
        max_edges: int,
    ) -> PrecedentTrace:
        matter, claim, finding = claim_context(matter_id, claim_id)
        cited = [
            link.case_id
            for link in finding.cited_authorities
            if not link.removed and link.citation_id is not None
        ]
        discovered = [
            evidence.result.case_id
            for evidence in finding.evidence
            if evidence.role == "independent_support"
        ]
        eligible = list(dict.fromkeys(cited + discovered))
        if seed_case_id is not None and seed_case_id not in eligible:
            raise MatterError("Seed authority is not linked to this claim.")
        seed = seed_case_id or next(iter(eligible), None)
        if seed is None:
            raise MatterError("Claim has no resolved authority to trace.")
        try:
            return PrecedentIntelligence(get_engine()).trace(
                seed,
                proposition=claim.normalized_proposition,
                forum_court=matter.court,
                matter_id=matter_id,
                as_of_date=as_of_date or matter.as_of_date,
                backward_depth=backward_depth,
                forward_depth=forward_depth,
                max_nodes=max_nodes,
                max_edges=max_edges,
            )
        except GraphError as error:
            raise HTTPException(status_code=503, detail=str(error)) from None

    @application.get("/cases/{case_id}/precedent-trace", response_model=PrecedentTrace)
    def case_precedent_trace(
        case_id: str,
        proposition: str | None = Query(default=None, max_length=4000),
        as_of_date: date | None = None,
        backward_depth: int = Query(default=1, ge=0, le=2),
        forward_depth: int = Query(default=1, ge=0, le=2),
        max_nodes: int = Query(default=20, ge=1, le=50),
        max_edges: int = Query(default=30, ge=1, le=100),
    ) -> PrecedentTrace:
        current = get_engine()
        if current.graph is None:
            raise HTTPException(status_code=503, detail="Citation graph unavailable.")
        try:
            return PrecedentIntelligence(current).trace(
                case_id,
                proposition=proposition,
                as_of_date=as_of_date,
                backward_depth=backward_depth,
                forward_depth=forward_depth,
                max_nodes=max_nodes,
                max_edges=max_edges,
            )
        except GraphError as error:
            raise HTTPException(status_code=404, detail=str(error)) from None

    @application.get(
        "/matters/{matter_id}/claims/{claim_id}/precedent-trace",
        response_model=PrecedentTrace,
    )
    def claim_precedent_trace(
        matter_id: str,
        claim_id: str,
        seed_case_id: str | None = None,
        as_of_date: date | None = None,
        backward_depth: int = Query(default=1, ge=0, le=2),
        forward_depth: int = Query(default=1, ge=0, le=2),
        max_nodes: int = Query(default=20, ge=1, le=50),
        max_edges: int = Query(default=30, ge=1, le=100),
    ) -> PrecedentTrace:
        return trace_for_claim(
            matter_id,
            claim_id,
            seed_case_id,
            as_of_date,
            backward_depth,
            forward_depth,
            max_nodes,
            max_edges,
        )

    @application.get(
        "/matters/{matter_id}/claims/{claim_id}/authority-analysis",
        response_model=ClaimAuthorityAnalysis,
    )
    def claim_authority_analysis(
        matter_id: str, claim_id: str
    ) -> ClaimAuthorityAnalysis:
        matter, _, finding = claim_context(matter_id, claim_id)
        relationships = []
        seen: set[str] = set()
        for evidence in finding.evidence:
            result = evidence.result
            if result.case_id in seen:
                continue
            seen.add(result.case_id)
            relationships.append(
                classify_authority(
                    result.case_id,
                    result.court,
                    matter.court,
                    matter_id=matter_id,
                )
            )
        return ClaimAuthorityAnalysis(
            matter_id=matter_id,
            claim_id=claim_id,
            forum_court=matter.court,
            relationships=relationships,
            warning="Court hierarchy does not prove relevance or binding issue fit.",
        )

    @application.get(
        "/matters/{matter_id}/claims/{claim_id}/doctrine",
        response_model=DoctrineAnalysis,
    )
    def claim_doctrine(
        matter_id: str,
        claim_id: str,
        seed_case_id: str | None = None,
        as_of_date: date | None = None,
        backward_depth: int = Query(default=1, ge=0, le=2),
        forward_depth: int = Query(default=1, ge=0, le=2),
        max_nodes: int = Query(default=20, ge=1, le=50),
        max_edges: int = Query(default=30, ge=1, le=100),
    ) -> DoctrineAnalysis:
        matter, claim, _ = claim_context(matter_id, claim_id)
        current = get_engine()
        graph = current.graph
        if graph is None:
            raise HTTPException(status_code=503, detail="Citation graph unavailable.")
        identity = hashlib.sha256(
            json.dumps(
                {
                    "version": TRACE_VERSION,
                    "treatment_version": TREATMENT_VERSION,
                    "claim": claim.normalized_proposition,
                    "seed": seed_case_id,
                    "graph": graph.metadata.graph_id,
                    "corpus": current.corpus.hash,
                    "passage_words": current.config.passages.words,
                    "forum": matter.court,
                    "as_of_date": str(as_of_date or matter.as_of_date),
                    "backward_depth": backward_depth,
                    "forward_depth": forward_depth,
                    "max_nodes": max_nodes,
                    "max_edges": max_edges,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        store = get_matter_store()
        cached = store.cached_doctrine(matter_id, claim_id, identity)
        if cached is not None:
            return cached.model_copy(update={"cache_hit": True})
        trace = trace_for_claim(
            matter_id,
            claim_id,
            seed_case_id,
            as_of_date,
            backward_depth,
            forward_depth,
            max_nodes,
            max_edges,
        )
        for edge in trace.edges:
            reviewed = store.treatment_review(
                matter_id, claim_id, edge.treatment.annotation_id
            )
            if reviewed is not None:
                edge.treatment = reviewed
        result = doctrine_for_claim(claim_id, claim.normalized_proposition, trace)
        store.save_doctrine(matter_id, claim_id, identity, result)
        return result

    @application.patch(
        "/matters/{matter_id}/claims/{claim_id}/treatments/{annotation_id}",
        response_model=TreatmentAnnotation,
    )
    def review_claim_treatment(
        matter_id: str,
        claim_id: str,
        annotation_id: str,
        request: TreatmentReview,
    ) -> TreatmentAnnotation:
        return get_matter_store().review_treatment(
            matter_id, claim_id, annotation_id, request.state
        )

    def research_context(
        matter_id: str, claim_id: str
    ) -> tuple[Matter, LegalClaim, ArgumentFinding]:
        return claim_context(matter_id, claim_id)

    def research_identity(
        matter: Matter, claim: LegalClaim, finding: ArgumentFinding
    ) -> str:
        source_versions: list[str] = []
        for root in (
            index_path or configured.index_path,
            graph_path or configured.graph_path,
        ):
            metadata = root / "metadata.json" if root is not None else None
            if metadata is not None and metadata.is_file():
                try:
                    source_versions.append(
                        hashlib.sha256(metadata.read_bytes()).hexdigest()
                    )
                except OSError:
                    source_versions.append("unavailable")
            else:
                source_versions.append("unavailable")
        return hashlib.sha256(
            json.dumps(
                {
                    "version": "matter-research-v2",
                    "claim": claim_fingerprint(claim),
                    "evidence": hashlib.sha256(
                        finding.model_dump_json().encode()
                    ).hexdigest(),
                    "forum": matter.court,
                    "as_of_date": str(matter.as_of_date),
                    "model": configured.llm_model,
                    "sources": source_versions,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()

    @application.get(
        "/matters/{matter_id}/claims/{claim_id}/research-coverage",
        response_model=ResearchCoverage,
    )
    def claim_research_coverage(matter_id: str, claim_id: str) -> ResearchCoverage:
        matter, claim, finding = research_context(matter_id, claim_id)
        store = get_matter_store()
        cached = store.research_coverage(claim_id)
        identity = research_identity(matter, claim, finding)
        if cached is not None and cached.cache_identity == identity:
            return cached
        latest = store.latest_research_run(matter_id, claim_id)
        coverage = assess_coverage(
            matter,
            claim,
            finding,
            latest.discoveries if latest else [],
            queries_executed=sum(len(r.queries) for r in latest.rounds)
            if latest
            else 0,
        )
        coverage.cache_identity = identity
        store.save_research_coverage(coverage)
        return coverage

    @application.post(
        "/matters/{matter_id}/claims/{claim_id}/research-plan",
        response_model=DeepResearchPlan,
    )
    def create_research_plan(matter_id: str, claim_id: str) -> DeepResearchPlan:
        matter, claim, _ = research_context(matter_id, claim_id)
        coverage = claim_research_coverage(matter_id, claim_id)
        plan = propose_plan(matter, claim, coverage)
        plan.context_identity = coverage.cache_identity
        get_matter_store().save_research_plan(plan)
        return plan

    @application.get(
        "/matters/{matter_id}/claims/{claim_id}/research-plan",
        response_model=DeepResearchPlan,
    )
    def get_research_plan(matter_id: str, claim_id: str) -> DeepResearchPlan:
        plan = get_matter_store().research_plan(matter_id, claim_id)
        if plan is None:
            raise MatterError("Research plan was not found.")
        return plan

    @application.patch(
        "/matters/{matter_id}/claims/{claim_id}/research-plan",
        response_model=DeepResearchPlan,
    )
    def edit_research_plan(
        matter_id: str, claim_id: str, request: PlanEdit
    ) -> DeepResearchPlan:
        store = get_matter_store()
        plan = store.research_plan(matter_id, claim_id)
        claim = store.claim(matter_id, claim_id)
        if plan is None or claim is None:
            raise MatterError("Research plan was not found.")
        if plan.status in {"RUNNING", "COMPLETED"}:
            raise MatterError("Running or completed plan cannot be edited.")
        if (
            plan.claim_fingerprint != claim_fingerprint(claim)
            or plan.context_identity
            != claim_research_coverage(matter_id, claim_id).cache_identity
        ):
            raise MatterError("Research plan is stale. Create a new plan.")
        if request.steps is not None:
            if len(request.steps) > 12 or any(
                step.claim_id != claim_id
                or not any(
                    gap.gap_id == step.gap_id
                    for gap in claim_research_coverage(matter_id, claim_id).gaps
                )
                for step in request.steps
            ):
                raise MatterError("Research steps must reference current claim gaps.")
            plan.steps = request.steps
        for key in (
            "max_rounds",
            "max_queries_per_round",
            "max_total_results",
            "max_duration_seconds",
        ):
            value = getattr(request, key)
            if value is not None:
                setattr(plan, key, value)
        plan.status = "APPROVED" if request.approve else "DRAFT"
        store.save_research_plan(plan)
        return plan

    @application.post(
        "/matters/{matter_id}/claims/{claim_id}/deep-research",
        status_code=202,
        response_model=RunAccepted,
    )
    def start_deep_research(
        matter_id: str, claim_id: str, gap_id: str | None = None
    ) -> RunAccepted:
        matter, claim, finding = research_context(matter_id, claim_id)
        store = get_matter_store()
        plan = store.research_plan(matter_id, claim_id)
        if plan is None or plan.status != "APPROVED":
            raise MatterError("Approve a research plan before running it.")
        if (
            plan.claim_fingerprint != claim_fingerprint(claim)
            or plan.context_identity
            != claim_research_coverage(matter_id, claim_id).cache_identity
        ):
            raise MatterError("Research plan is stale. Create a new plan.")
        execution_plan = plan
        if gap_id is not None:
            selected = [
                step for step in plan.steps if step.gap_id == gap_id and step.approved
            ]
            if not selected:
                raise MatterError("Approved research gap was not found.")
            execution_plan = plan.model_copy(update={"steps": selected})
        if not matter_slots.acquire(blocking=False):
            raise MatterError("Matter analysis queue is full.")
        run = DeepResearchRun(
            run_id=uuid.uuid4().hex,
            plan_id=plan.plan_id,
            matter_id=matter_id,
            claim_id=claim_id,
            status="queued",
        )
        try:
            store.save_research_run(run)
            plan.status = "RUNNING"
            store.save_research_plan(plan)

            def perform() -> None:
                try:
                    run.status = "running"
                    run.started_at = datetime.now(UTC)
                    store.save_research_run(run)
                    approved = execution_plan.model_copy(update={"status": "APPROVED"})
                    state = cast(
                        DeepResearchState,
                        deep_research_graph(
                            get_engine(),
                            progress=store.save_research_run,
                            stopped=lambda: (
                                (current := store.research_run(matter_id, run.run_id))
                                is not None
                                and current.status == "stopped"
                            ),
                        ).invoke(
                            {
                                "matter": matter,
                                "claim": claim,
                                "finding": finding,
                                "plan": approved,
                                "run": run,
                                "coverage": None,
                            }
                        ),
                    )
                    store.save_research_run(state["run"])
                    if state["run"].coverage is not None:
                        state["run"].coverage.cache_identity = research_identity(
                            matter, claim, finding
                        )
                        store.save_research_coverage(state["run"].coverage)
                    plan.status = (
                        "STOPPED"
                        if state["run"].stop_reason == "STOPPED"
                        else "COMPLETED"
                    )
                    store.save_research_plan(plan)
                except Exception:
                    run.status = "failed"
                    run.stop_reason = "ERROR"
                    run.warning = (
                        "Research failed; no private request content was logged."
                    )
                    run.completed_at = datetime.now(UTC)
                    store.save_research_run(run)
                    plan.status = "STOPPED"
                    store.save_research_plan(plan)
                finally:
                    matter_slots.release()

            matter_executor.submit(perform)
        except Exception:
            matter_slots.release()
            raise
        return RunAccepted(run_id=run.run_id)

    @application.get(
        "/matters/{matter_id}/research-runs/{run_id}",
        response_model=DeepResearchRun,
    )
    def get_deep_research_run(matter_id: str, run_id: str) -> DeepResearchRun:
        run = get_matter_store().research_run(matter_id, run_id)
        if run is None:
            raise MatterError("Research run was not found.")
        return run

    @application.post(
        "/matters/{matter_id}/research-runs/{run_id}/stop",
        response_model=DeepResearchRun,
    )
    def stop_deep_research(matter_id: str, run_id: str) -> DeepResearchRun:
        store = get_matter_store()
        run = store.research_run(matter_id, run_id)
        if run is None:
            raise MatterError("Research run was not found.")
        if run.status in {"queued", "running"}:
            run.status = "stopped"
            run.stop_reason = "STOPPED"
            store.save_research_run(run)
        return run

    @application.get(
        "/matters/{matter_id}/claims/{claim_id}/attacks",
        response_model=list[AttackFinding],
    )
    def claim_attacks(matter_id: str, claim_id: str) -> list[AttackFinding]:
        if get_matter_store().claim(matter_id, claim_id) is None:
            raise MatterError("Claim was not found.")
        return get_matter_store().attacks(matter_id, claim_id)

    @application.post(
        "/matters/{matter_id}/claims/{claim_id}/red-team",
        status_code=202,
    )
    def start_red_team(matter_id: str, claim_id: str) -> dict[str, str]:
        matter, claim, finding = research_context(matter_id, claim_id)
        store = get_matter_store()
        if not matter_slots.acquire(blocking=False):
            raise MatterError("Matter analysis queue is full.")
        try:
            job_id = store.create_job(matter_id, claim.document_id, claim_id)
            red_run = DeepResearchRun(
                run_id=job_id,
                plan_id="red-team",
                matter_id=matter_id,
                claim_id=claim_id,
                status="queued",
            )
            store.save_research_run(red_run)

            def perform() -> None:
                private_cache: StructuredCache | None = None
                started = time.monotonic()
                try:
                    store.set_job(job_id, "running")
                    red_run.status = "running"
                    red_run.started_at = datetime.now(UTC)
                    store.save_research_run(red_run)
                    coverage = claim_research_coverage(matter_id, claim_id)
                    verifier = None
                    if configured.llm_model:
                        analyzer, private_cache = get_matter_analyzer(matter_id)
                        verifier = cast(CachedStructuredLLM, analyzer.llm)
                    state = cast(
                        RedTeamState,
                        red_team_graph(get_engine(), llm=verifier).invoke(
                            {
                                "matter": matter,
                                "claim": claim,
                                "finding": finding,
                                "coverage": coverage,
                                "run": red_run,
                                "sections": store.sections(
                                    matter_id, claim.document_id
                                ),
                                "doctrine": store.latest_doctrine(matter_id, claim_id),
                                "attacks": [],
                            }
                        ),
                    )
                    store.save_attacks(
                        matter_id,
                        claim_id,
                        state["attacks"],
                    )
                    state["run"].status = "completed"
                    state["run"].completed_at = datetime.now(UTC)
                    state["run"].elapsed_ms = int((time.monotonic() - started) * 1000)
                    state["run"].attack_hypotheses = len(state["attacks"])
                    state["run"].verified_attacks = sum(
                        attack.verified for attack in state["attacks"]
                    )
                    state["run"].unverified_attacks = sum(
                        not attack.verified for attack in state["attacks"]
                    )
                    if verifier is not None:
                        state["run"].cache_hits = verifier.hits
                        state["run"].llm_calls = verifier.usage.calls
                        state["run"].input_tokens = verifier.usage.input_tokens
                        state["run"].output_tokens = verifier.usage.output_tokens
                    store.save_research_run(state["run"])
                    store.set_job(job_id, "completed")
                except Exception:
                    red_run.status = "failed"
                    red_run.stop_reason = "ERROR"
                    red_run.warning = "Red Team research failed."
                    red_run.completed_at = datetime.now(UTC)
                    red_run.elapsed_ms = int((time.monotonic() - started) * 1000)
                    store.save_research_run(red_run)
                    store.set_job(job_id, "failed", "RED_TEAM_FAILED")
                finally:
                    if private_cache is not None:
                        private_cache.close()
                    matter_slots.release()

            matter_executor.submit(perform)
        except Exception:
            matter_slots.release()
            raise
        return {"job_id": job_id, "status": "queued"}

    @application.get(
        "/matters/{matter_id}/attack-surface", response_model=AttackSurface
    )
    def matter_attack_surface(matter_id: str) -> AttackSurface:
        if get_matter_store().get_matter(matter_id) is None:
            raise MatterError("Matter was not found.")
        return attack_surface(matter_id, get_matter_store().attacks(matter_id))

    @application.get(
        "/matters/{matter_id}/evidence-matrix", response_model=list[EvidenceMatrixRow]
    )
    def evidence_matrix(
        matter_id: str,
        issue: str | None = None,
        status: str | None = None,
        authority: str | None = None,
        citation_support: str | None = None,
        coverage: str | None = None,
        severity: str | None = None,
        unresolved_only: bool = False,
        document: str | None = None,
        sort: Literal[
            "vulnerability", "coverage", "issue", "authority", "importance"
        ] = "vulnerability",
    ) -> list[EvidenceMatrixRow]:
        return filter_matrix(
            matrix_rows(get_matter_store(), matter_id),
            issue=issue,
            status=status,
            authority=authority,
            citation_support=citation_support,
            coverage=coverage,
            severity=severity,
            unresolved_only=unresolved_only,
            document=document,
            sort=sort,
        )

    @application.get("/matters/{matter_id}/evidence-matrix.csv")
    def evidence_matrix_export(matter_id: str) -> Response:
        store = get_matter_store()
        matter = store.get_matter(matter_id)
        if matter is None:
            raise MatterError("Matter was not found.")
        return Response(
            content=matrix_csv(matrix_rows(store, matter_id), matter.name),
            media_type="text/csv",
            headers={
                "Content-Disposition": 'attachment; filename="evidence-matrix.csv"'
            },
        )

    return application


app = create_app()

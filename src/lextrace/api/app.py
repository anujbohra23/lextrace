"""Production-style local API with bounded background legal research."""

import hashlib
import json
import threading
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

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
from lextrace.matter.documents import MAX_UPLOAD_BYTES
from lextrace.matter.store import MatterStore
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
                    claim = store.claim(matter_id, claim_id)
                    if claim is None:
                        raise MatterError("Claim was not found.")
                    citations = store.analysis(matter_id, document_id).citations
                    previous = store.finding(matter_id, claim_id)
                    finding = analyzer.analyze_claim(claim, citations)
                    if previous is not None:
                        reviews = {
                            link.evidence_id: link
                            for link in previous.cited_authorities
                        }
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
        if request.normalized_proposition is not None:
            proposition = request.normalized_proposition.strip()
            if not proposition or len(proposition) > 1000:
                raise MatterError("Normalized proposition is invalid.")
            claim.normalized_proposition = proposition
            claim.manually_edited = True
        if request.irrelevant is not None:
            claim.irrelevant = request.irrelevant
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

    return application


app = create_app()

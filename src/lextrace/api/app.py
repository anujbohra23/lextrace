"""Production-style local API with bounded background legal research."""

import threading
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware

from lextrace.config import (
    APP_TITLE,
    AppSettings,
    ConfigurationError,
    openai_api_key,
)
from lextrace.domain.case import Case
from lextrace.graph.contracts import CitationNeighbor, GraphError
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

    application = FastAPI(title=APP_TITLE, lifespan=lifespan)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=configured.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["content-type", "x-request-id"],
    )

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

    return application


app = create_app()

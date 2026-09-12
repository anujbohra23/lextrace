"""Local retrieval API. Health is independent of corpus/model availability."""

import os
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query

from lextrace.config import APP_TITLE
from lextrace.domain.case import Case
from lextrace.graph.contracts import CitationNeighbor, GraphError
from lextrace.research.contracts import ResearchError, ResearchRequest, ResearchResponse
from lextrace.retrieval.contracts import RetrievalError, SearchRequest, SearchResponse
from lextrace.retrieval.engine import LexTraceRetriever


def create_app(
    retriever: LexTraceRetriever | None = None,
    *,
    index_path: Path | None = None,
    graph_path: Path | None = None,
    research_workflow: Any | None = None,
) -> FastAPI:
    lock = threading.Lock()
    engine = retriever

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        yield
        if engine is not None:
            engine.close()
        research_engine = getattr(research_workflow, "retriever", None)
        if (
            isinstance(research_engine, LexTraceRetriever)
            and research_engine is not engine
        ):
            research_engine.close()

    application = FastAPI(title=APP_TITLE, lifespan=lifespan)

    def get_engine() -> LexTraceRetriever:
        nonlocal engine
        with lock:
            if engine is None:
                try:
                    engine = LexTraceRetriever.from_index(
                        index_path
                        or Path(
                            os.environ.get(
                                "LEXTRACE_INDEX_PATH", "artifacts/indexes/default"
                            )
                        ),
                        graph_path=graph_path
                        or (
                            Path(value)
                            if (value := os.environ.get("LEXTRACE_GRAPH_PATH"))
                            else None
                        ),
                    )
                except (RetrievalError, GraphError) as error:
                    raise HTTPException(status_code=503, detail=str(error)) from None
        return engine

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.post("/search", response_model=SearchResponse)
    def search(request: SearchRequest) -> SearchResponse:
        try:
            return get_engine().search_response(request)
        except RetrievalError as error:
            raise HTTPException(status_code=503, detail=str(error)) from None

    @application.post("/research", response_model=ResearchResponse)
    def research(request: ResearchRequest) -> ResearchResponse:
        nonlocal research_workflow
        try:
            if research_workflow is None:
                from lextrace.research.commands import build_workflow

                configured_index = index_path or Path(
                    os.environ.get("LEXTRACE_INDEX_PATH", "artifacts/indexes/default")
                )
                research_workflow = build_workflow(
                    configured_index,
                    graph_path,
                    os.environ.get("LEXTRACE_LLM_MODEL"),
                )
            return research_workflow.run(request)
        except (ResearchError, RetrievalError, GraphError) as error:
            raise HTTPException(status_code=503, detail=str(error)) from None

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

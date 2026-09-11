"""Local retrieval API. Health is independent of corpus/model availability."""

import os
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException

from lextrace.config import APP_TITLE
from lextrace.domain.case import Case
from lextrace.retrieval.contracts import RetrievalError, SearchRequest, SearchResponse
from lextrace.retrieval.engine import LexTraceRetriever


def create_app(
    retriever: LexTraceRetriever | None = None, *, index_path: Path | None = None
) -> FastAPI:
    lock = threading.Lock()
    engine = retriever

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        yield
        if engine is not None:
            engine.close()

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
                        )
                    )
                except RetrievalError as error:
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

    @application.get("/cases/{case_id}", response_model=Case)
    def get_case(case_id: str) -> Case:
        case = get_engine().corpus.by_id.get(case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="Case not found.")
        return case

    return application


app = create_app()

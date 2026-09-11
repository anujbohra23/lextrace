"""High-level local case search. No CourtListener requests or benchmark labels."""

import logging
import time
import uuid

from lextrace.retrieval.bm25 import BM25, BM25Config
from lextrace.retrieval.contracts import (
    Diagnostics,
    Mode,
    RetrievalError,
    RetrievalResult,
    RetrievalTrace,
    SearchFilters,
    SearchRequest,
    SearchResponse,
)
from lextrace.retrieval.documents import Corpus
from lextrace.retrieval.passages import segment, select
from lextrace.retrieval.settings import EngineConfig

logger = logging.getLogger("lextrace.retrieval")


class LexTraceRetriever:
    def __init__(
        self, corpus: Corpus, config: EngineConfig | None = None, *, mode: Mode = "bm25"
    ) -> None:
        self.corpus = corpus
        self.config = config or EngineConfig()
        self.mode = mode
        self.lexical = BM25(
            corpus.cases, BM25Config(k1=self.config.bm25.k1, b=self.config.bm25.b)
        )

    def search(
        self, query: str, top_k: int = 10, filters: SearchFilters | None = None
    ) -> list[RetrievalResult]:
        return self.search_response(
            SearchRequest(query=query, top_k=top_k, mode=self.mode, filters=filters)
        ).results

    def search_response(self, request: SearchRequest) -> SearchResponse:
        start = time.perf_counter()
        if request.mode != "bm25":
            raise RetrievalError("This mode requires the dense retrieval pipeline.")
        eligible = self.corpus.eligible(request.filters)
        trace = RetrievalTrace(
            request_id=uuid.uuid4().hex,
            mode=request.mode,
            corpus_hash=self.corpus.hash,
            index_version="1",
            query_length=len(request.query),
        )
        before = time.perf_counter()
        ranked = [
            r
            for r in self.lexical.rank(trace.request_id, request.query, "engine-v1")
            if r.case_id in eligible
        ]
        trace.stage_seconds["bm25"] = time.perf_counter() - before
        trace.candidate_counts["bm25"] = len(ranked)
        results: list[RetrievalResult] = []
        before = time.perf_counter()
        for row in ranked[: request.top_k]:
            case = self.corpus.by_id[row.case_id]
            passage = select(
                segment(case, self.config.passages), request.query, self.lexical.idf, 1
            )[0]
            results.append(
                RetrievalResult(
                    case_id=case.source_id,
                    rank=len(results) + 1,
                    final_score=row.score,
                    retrieval_method=request.mode,
                    case_name=case.name,
                    court=case.court_id,
                    date_filed=case.date_filed,
                    reporter_citations=case.reporter_citations,
                    source_url=case.source_url,
                    relevant_passage=passage,
                    diagnostics=Diagnostics(
                        bm25_rank=len(results) + 1, bm25_score=row.score
                    ),
                )
            )
        trace.stage_seconds["passages"] = time.perf_counter() - before
        trace.stage_seconds["total"] = time.perf_counter() - start
        logger.info(trace.model_dump_json())
        return SearchResponse(results=results, trace=trace)

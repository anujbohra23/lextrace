"""Local case retrieval: lexical/dense → RRF → passage cross-encoder → results."""

import logging
import math
import threading
import time
import uuid
from pathlib import Path

from lextrace.evaluation.benchmark import BenchmarkError
from lextrace.graph.contracts import GraphError
from lextrace.graph.store import CitationGraph
from lextrace.retrieval.bm25 import BM25, BM25Config
from lextrace.retrieval.contracts import (
    CitationProvenance,
    Diagnostics,
    Mode,
    Passage,
    RetrievalError,
    RetrievalResult,
    RetrievalTrace,
    SearchFilters,
    SearchRequest,
    SearchResponse,
)
from lextrace.retrieval.dense import dense_search
from lextrace.retrieval.documents import Corpus
from lextrace.retrieval.fusion import rrf
from lextrace.retrieval.index import LocalIndex, representation
from lextrace.retrieval.models import (
    CrossScorer,
    Encoder,
    PairScorer,
    SentenceEncoder,
    Vector,
)
from lextrace.retrieval.passages import segment, select
from lextrace.retrieval.settings import EngineConfig

logger = logging.getLogger("lextrace.retrieval")


class LexTraceRetriever:
    def __init__(
        self,
        corpus: Corpus,
        config: EngineConfig | None = None,
        *,
        mode: Mode | None = None,
        vectors: Vector | None = None,
        encoder: Encoder | None = None,
        scorer: PairScorer | None = None,
        graph: CitationGraph | None = None,
    ) -> None:
        self.corpus = corpus
        self.config = config or EngineConfig()
        self.mode = mode or self.config.default_mode
        try:
            self.lexical = BM25(
                corpus.cases, BM25Config(k1=self.config.bm25.k1, b=self.config.bm25.b)
            )
        except BenchmarkError:
            raise RetrievalError("Corpus has no valid searchable documents.") from None
        self.vectors = vectors
        if vectors is not None and (
            vectors.ndim != 2 or vectors.shape[0] != len(corpus.ids)
        ):
            raise RetrievalError("Dense rows do not align with the corpus.")
        self.encoder = encoder or SentenceEncoder(self.config.dense)
        self.scorer = scorer or CrossScorer(self.config.reranker)
        self.graph = graph
        self._lock = threading.RLock()

    @classmethod
    def from_index(
        cls,
        path: Path,
        *,
        corpus_path: Path | None = None,
        config: EngineConfig | None = None,
        graph_path: Path | None = None,
    ) -> "LexTraceRetriever":
        index = LocalIndex(path, corpus_path=corpus_path, config=config)
        graph = CitationGraph(graph_path) if graph_path is not None else None
        return cls(index.corpus, index.config, vectors=index.vectors, graph=graph)

    def close(self) -> None:
        with self._lock:
            if isinstance(self.encoder, SentenceEncoder):
                self.encoder.close()
            if isinstance(self.scorer, CrossScorer):
                self.scorer.close()
            if self.graph is not None:
                self.graph.close()
                self.graph = None

    def search(
        self, query: str, top_k: int = 10, filters: SearchFilters | None = None
    ) -> list[RetrievalResult]:
        return self.search_response(
            SearchRequest(query=query, top_k=top_k, mode=self.mode, filters=filters)
        ).results

    def search_response(self, request: SearchRequest) -> SearchResponse:
        # Serialize local model use; traces and results stay request-local.
        waiting = time.perf_counter()
        with self._lock:
            queue_seconds = time.perf_counter() - waiting
            response = self._search(request)
            response.trace.stage_seconds["queue"] = queue_seconds
            response.trace.stage_seconds["total"] += queue_seconds
            logger.info(response.trace.model_dump_json())
            return response

    def _search(self, request: SearchRequest) -> SearchResponse:
        start = time.perf_counter()
        config = self.config
        eligible = self.corpus.eligible(request.filters)
        trace = RetrievalTrace(
            request_id=uuid.uuid4().hex,
            mode=request.mode,
            corpus_hash=self.corpus.hash,
            index_version="1:" + representation(config)[:16],
            query_length=len(request.query),
            candidate_counts={
                key: 0 for key in ("bm25", "dense", "hybrid", "reranked")
            },
            stage_seconds={
                key: 0.0
                for key in (
                    "bm25",
                    "dense",
                    "fusion",
                    "graph",
                    "reranker",
                    "passages",
                    "total",
                )
            },
            embedding_model=None
            if request.mode == "bm25"
            else config.dense.model + "@" + config.dense.revision,
            reranker_model=config.reranker.model + "@" + config.reranker.revision
            if request.mode in {"reranked", "citation_reranked"}
            else None,
            graph_id=self.graph.metadata.graph_id if self.graph is not None else None,
        )
        diagnostics: dict[str, Diagnostics] = {}
        lexical: list[tuple[str, float]] = []
        dense: list[tuple[str, float]] = []
        if eligible and request.mode != "dense":
            before = time.perf_counter()
            depth = (
                request.top_k if request.mode == "bm25" else config.hybrid.lexical_depth
            )
            rows = self.lexical.rank(
                trace.request_id,
                request.query,
                "engine-v1",
                case_ids=eligible,
                top_k=depth,
            )
            lexical = [(r.case_id, r.score) for r in rows]
            for r in rows:
                diagnostics[r.case_id] = Diagnostics(
                    bm25_rank=r.rank, bm25_score=r.score
                )
            trace.stage_seconds["bm25"] = time.perf_counter() - before
            trace.candidate_counts["bm25"] = len(lexical)
        if eligible and request.mode != "bm25":
            if self.vectors is None:
                raise RetrievalError(
                    "Dense mode requires a dense index; run build-index first."
                )
            before = time.perf_counter()
            try:
                query = self.encoder.encode([request.query])
            except RetrievalError:
                raise
            except Exception:
                raise RetrievalError("Embedding inference failed.") from None
            depth = (
                request.top_k if request.mode == "dense" else config.hybrid.dense_depth
            )
            dense = dense_search(
                self.vectors,
                self.corpus.ids,
                query,
                eligible,
                depth,
                config.dense.block_size,
            )
            for rank, (identifier, score) in enumerate(dense, 1):
                diagnostic = diagnostics.setdefault(identifier, Diagnostics())
                diagnostic.dense_rank = rank
                diagnostic.dense_score = score
            trace.stage_seconds["dense"] = time.perf_counter() - before
            trace.candidate_counts["dense"] = len(dense)
        ranked = lexical if request.mode == "bm25" else dense
        if request.mode in {"hybrid", "reranked", "citation_reranked"}:
            before = time.perf_counter()
            ranked = rrf(
                [i for i, _ in lexical],
                [i for i, _ in dense],
                config.hybrid.rrf_constant,
            )[: config.hybrid.candidate_depth]
            for identifier, score in ranked:
                diagnostics[identifier].hybrid_score = score
            trace.stage_seconds["fusion"] = time.perf_counter() - before
            trace.candidate_counts["hybrid"] = len(ranked)
        if request.mode == "citation_reranked":
            if not config.graph.enabled or self.graph is None:
                raise RetrievalError(
                    "Citation reranking requires a configured citation graph."
                )
            before = time.perf_counter()
            seeds = [identifier for identifier, _ in ranked[: config.graph.seed_count]]
            try:
                expansion = self.graph.expand(
                    seeds,
                    hops=config.graph.hops,
                    direction=config.graph.direction,
                    max_nodes=config.graph.max_expanded_candidates,
                    as_of_date=request.filters.filed_before
                    if request.filters and config.graph.as_of_from_filed_before
                    else None,
                )
            except GraphError:
                raise RetrievalError("Citation graph expansion failed.") from None
            combined = list(seeds)
            for neighbor in expansion.neighbors:
                identifier = neighbor.node.case_id
                if identifier not in eligible:
                    continue
                if identifier not in seeds:
                    diagnostic = diagnostics.setdefault(identifier, Diagnostics())
                    diagnostic.discovered_via_citation = True
                    for path in expansion.provenance[identifier][
                        : config.graph.provenance_limit
                    ]:
                        diagnostic.citation_provenance.append(
                            CitationProvenance(
                                seed_case_id=path.seed_case_id or identifier,
                                candidate_case_id=identifier,
                                direction=path.direction,
                                hop=path.hop,
                                citing_case_id=path.edge.citing_case_id,
                                cited_case_id=path.edge.cited_case_id,
                                supporting_opinion_edge_count=(
                                    path.edge.supporting_opinion_edge_count
                                ),
                                provenance_ids=[
                                    support.provenance_id
                                    for support in path.edge.supports
                                ],
                            )
                        )
                if identifier not in combined:
                    combined.append(identifier)
                if len(combined) >= config.graph.max_expanded_candidates:
                    break
            score_by_id = dict(ranked)
            ranked = [
                (identifier, score_by_id.get(identifier, 0.0))
                for identifier in combined
            ]
            trace.stage_seconds["graph"] = time.perf_counter() - before
            trace.candidate_counts["graph_discovered"] = expansion.discovered_count
            trace.candidate_counts["graph_local"] = len(combined) - len(seeds)
            trace.candidate_counts["graph_deduplicated"] = expansion.deduplicated_count
        candidates = (
            ranked[: config.graph.max_expanded_candidates]
            if request.mode == "citation_reranked"
            else ranked[: config.reranker.candidate_depth]
            if request.mode == "reranked"
            else ranked[: request.top_k]
        )
        before = time.perf_counter()
        passages: dict[str, list[Passage]] = {
            identifier: select(
                segment(self.corpus.by_id[identifier], config.passages),
                request.query,
                self.lexical.idf,
                config.passages.evidence_count,
            )
            for identifier, _ in candidates
        }
        trace.stage_seconds["passages"] = time.perf_counter() - before
        if request.mode in {"reranked", "citation_reranked"} and candidates:
            before = time.perf_counter()
            pairs = [
                (request.query, p.text)
                for identifier, _ in candidates
                for p in passages[identifier]
            ]
            try:
                values = []
                for offset in range(0, len(pairs), config.reranker.batch_size):
                    batch = pairs[offset : offset + config.reranker.batch_size]
                    scores = self.scorer.score(batch)
                    if len(scores) != len(batch) or any(
                        not math.isfinite(v) for v in scores
                    ):
                        raise RetrievalError("Reranker returned invalid scores.")
                    values.extend(scores)
            except RetrievalError:
                raise
            except Exception:
                raise RetrievalError(
                    "Reranking failed; no substitute ranking was returned."
                ) from None
            offset = 0
            for identifier, _ in candidates:
                selected = passages[identifier]
                for passage, score in zip(
                    selected, values[offset : offset + len(selected)], strict=True
                ):
                    passage.score = score
                    passage.scoring_method = "cross-encoder"
                selected.sort(key=lambda p: -p.score)
                diagnostics[identifier].reranker_score = selected[0].score
                offset += len(selected)
            ranked = sorted(
                ((i, passages[i][0].score) for i, _ in candidates),
                key=lambda row: (-row[1], int(row[0])),
            )
            trace.candidate_counts["reranked"] = len(ranked)
            trace.stage_seconds["reranker"] = time.perf_counter() - before
        results: list[RetrievalResult] = []
        for identifier, score in ranked[: request.top_k]:
            case = self.corpus.by_id[identifier]
            results.append(
                RetrievalResult(
                    case_id=identifier,
                    rank=len(results) + 1,
                    final_score=score,
                    retrieval_method=request.mode,
                    case_name=case.name,
                    court=case.court_id,
                    date_filed=case.date_filed,
                    reporter_citations=case.reporter_citations,
                    source_url=case.source_url,
                    relevant_passage=passages[identifier][0],
                    diagnostics=diagnostics[identifier],
                )
            )
        trace.stage_seconds["total"] = time.perf_counter() - start
        return SearchResponse(results=results, trace=trace)

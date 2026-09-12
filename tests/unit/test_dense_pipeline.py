"""Offline dense/cache/fusion/reranking contracts using injected models."""

from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest

from lextrace.corpus import serialize_cases
from lextrace.domain.case import Case
from lextrace.evaluation.benchmark import (
    BenchmarkConfig,
    BenchmarkInputs,
    CitationEvidence,
    OpinionMapping,
    digest,
)
from lextrace.graph.build import build_graph
from lextrace.graph.contracts import GraphEvidenceBundle
from lextrace.graph.store import CitationGraph
from lextrace.retrieval.contracts import RetrievalError, SearchFilters, SearchRequest
from lextrace.retrieval.documents import Corpus
from lextrace.retrieval.engine import LexTraceRetriever
from lextrace.retrieval.fusion import rrf
from lextrace.retrieval.index import LocalIndex, build_index
from lextrace.retrieval.models import Vector, unit
from lextrace.retrieval.settings import EngineConfig

Sample = tuple[list[Case], BenchmarkInputs, BenchmarkConfig]


class FakeEncoder:
    def __init__(self) -> None:
        self.calls = 0

    def encode(self, texts: Sequence[str]) -> Vector:
        self.calls += 1
        return unit(
            np.asarray(
                [
                    [
                        float(text.count(word))
                        for word in ("salary", "search", "contract")
                    ]
                    + [0.1]
                    for text in texts
                ],
                dtype=np.float32,
            )
        )


class FakeScorer:
    def __init__(self) -> None:
        self.batches: list[list[tuple[str, str]]] = []

    def score(self, pairs: Sequence[tuple[str, str]]) -> list[float]:
        self.batches.append(list(pairs))
        return [9.0 if "contract" in text else 2.0 for _, text in pairs]


@pytest.fixture
def local_cases(benchmark_sample: Sample) -> list[Case]:
    cases, _, _ = benchmark_sample
    return [
        c.model_copy(
            update={"opinions": [c.opinions[0].model_copy(update={"text": text})]}
        )
        for c, text in zip(
            cases[:3],
            [
                "salary employee wages",
                "search police seizure",
                "contract damages breach",
            ],
            strict=True,
        )
    ]


def test_rrf_missing_duplicate_and_ties() -> None:
    rows = rrf(["1", "1", "2"], ["2", "3"], 60)
    assert rows[0] == ("2", 1 / 62 + 1 / 61)
    assert dict(rows)["1"] == 1 / 61
    assert dict(rows)["3"] == 1 / 62
    assert rrf(["2"], ["1"]) == [("1", 1 / 61), ("2", 1 / 61)]


def test_index_cache_alignment(
    local_cases: list[Case], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    source = Path("source.jsonl")
    source.write_text(serialize_cases(local_cases))
    output = Path("artifacts/indexes/example")
    encoder = FakeEncoder()
    metadata = build_index(source, output, encoder=encoder)
    assert metadata.document_count == 3
    count = encoder.calls
    assert build_index(source, output, encoder=encoder) == metadata
    assert encoder.calls == count
    loaded = LocalIndex(output, corpus_path=source)
    assert loaded.vectors is not None and loaded.vectors.shape == (3, 4)
    source.write_text(serialize_cases(local_cases[:2]))
    with pytest.raises(RetrievalError, match="corpus"):
        LocalIndex(output, corpus_path=source)
    config = EngineConfig()
    config.dense.revision = "a" * 40
    with pytest.raises(RetrievalError, match="mismatch"):
        LocalIndex(output, config=config)
    for field, value in (("window_tokens", 128), ("window_overlap_tokens", 16)):
        config = EngineConfig()
        setattr(config.dense, field, value)
        with pytest.raises(RetrievalError, match="mismatch"):
            LocalIndex(output, config=config)
    (output / "document_map.json").write_text('["3","2","1"]')
    with pytest.raises(RetrievalError, match="checksums"):
        LocalIndex(output)


@pytest.mark.parametrize("mode", ["bm25", "dense", "hybrid", "reranked"])
def test_modes(local_cases: list[Case], mode: str) -> None:
    config = EngineConfig()
    config.reranker.batch_size = 1
    corpus = Corpus(local_cases)
    encoder = FakeEncoder()
    scorer = FakeScorer()
    vectors = encoder.encode([corpus.text(i) for i in corpus.ids])
    engine = LexTraceRetriever(
        corpus, config, vectors=vectors, encoder=encoder, scorer=scorer
    )
    request = SearchRequest.model_validate(
        {"query": "salary", "top_k": 2, "mode": mode}
    )
    response = engine.search_response(request)
    assert (
        len(response.results) == 2 and len({r.case_id for r in response.results}) == 2
    )
    assert [r.rank for r in response.results] == [1, 2]
    assert all(r.relevant_passage.text for r in response.results)
    assert response.results == engine.search_response(request).results
    if mode == "reranked":
        assert response.results[0].case_id == "3"
        assert response.results[0].diagnostics.reranker_score == 9
        assert all(len(batch) == 1 for batch in scorer.batches)
    else:
        assert response.results[0].case_id == "1"
    filtered = engine.search_response(
        request.model_copy(update={"filters": SearchFilters(courts=["scotus"])})
    )
    assert all(r.court == "scotus" for r in filtered.results)


def test_reranker_depth_and_failure(local_cases: list[Case]) -> None:
    corpus = Corpus(local_cases)
    encoder = FakeEncoder()
    scorer = FakeScorer()
    config = EngineConfig()
    config.reranker.candidate_depth = 1
    engine = LexTraceRetriever(
        corpus,
        config,
        vectors=encoder.encode([corpus.text(i) for i in corpus.ids]),
        encoder=encoder,
        scorer=scorer,
    )
    result = engine.search_response(SearchRequest(query="salary", top_k=3))
    assert len(result.results) == 1 and result.trace.candidate_counts["reranked"] == 1

    class Broken:
        def score(self, pairs: Sequence[tuple[str, str]]) -> list[float]:
            raise ValueError("private query secret")

    engine.scorer = Broken()
    with pytest.raises(RetrievalError) as exc:
        engine.search("salary")
    assert "secret" not in str(exc.value)


def test_no_dense_or_no_matches(local_cases: list[Case]) -> None:
    engine = LexTraceRetriever(Corpus(local_cases))
    with pytest.raises(RetrievalError, match="dense index"):
        engine.search("salary")
    assert engine.search("salary", filters=SearchFilters(courts=["absent"])) == []


def test_failed_build_is_atomic(
    local_cases: list[Case], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    source = Path("source.jsonl")
    source.write_text(serialize_cases(local_cases))

    class Broken:
        def encode(self, texts: Sequence[str]) -> Vector:
            raise ValueError("private input")

    output = Path("artifacts/indexes/broken")
    with pytest.raises(RetrievalError):
        build_index(source, output, encoder=Broken())
    assert not output.exists()


def test_citation_expansion_adds_only_local_candidates(
    local_cases: list[Case], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    corpus_path = Path("cases.jsonl")
    corpus_path.write_text(serialize_cases(local_cases))
    citing_opinion = local_cases[0].opinions[0].source_id
    bundle = GraphEvidenceBundle(
        citations=[
            CitationEvidence(
                citing_opinion_id=citing_opinion,
                cited_opinion_ids=["700", "701"],
                relation_source="opinions-cited",
                payload_sha256=digest("evidence"),
                depths={"700": 2, "701": 1},
                complete=True,
            )
        ],
        opinion_mappings=[
            OpinionMapping(
                opinion_id="700",
                case_id=local_cases[2].source_id,
                date_filed=local_cases[2].date_filed,
                court=local_cases[2].court_id,
                case_name=local_cases[2].name,
                payload_sha256=digest("700"),
            ),
            OpinionMapping(
                opinion_id="701",
                case_id="999",
                date_filed=local_cases[2].date_filed,
                court="ca2",
                case_name="Unavailable locally",
                payload_sha256=digest("701"),
            ),
        ],
        source_provenance="offline-test",
    )
    source = Path("citations.json")
    source.write_text(bundle.model_dump_json())
    path = Path("artifacts/graphs/test")
    build_graph(corpus_path, source, path)
    corpus = Corpus(local_cases)
    encoder = FakeEncoder()
    config = EngineConfig()
    config.graph.seed_count = 1
    config.graph.max_expanded_candidates = 5
    engine = LexTraceRetriever(
        corpus,
        config,
        vectors=encoder.encode([corpus.text(i) for i in corpus.ids]),
        encoder=encoder,
        scorer=FakeScorer(),
        graph=CitationGraph(path),
    )
    response = engine.search_response(
        SearchRequest(query="salary", top_k=3, mode="citation_reranked")
    )
    assert response.results[0].case_id == local_cases[2].source_id
    assert response.results[0].diagnostics.discovered_via_citation
    provenance = response.results[0].diagnostics.citation_provenance[0]
    assert provenance.seed_case_id == local_cases[0].source_id
    assert provenance.candidate_case_id == local_cases[2].source_id
    assert "999" not in {result.case_id for result in response.results}
    assert response.trace.candidate_counts["graph_local"] == 1
    assert response.trace.graph_id is not None
    engine.close()


def test_citation_mode_requires_graph(local_cases: list[Case]) -> None:
    corpus = Corpus(local_cases)
    encoder = FakeEncoder()
    engine = LexTraceRetriever(
        corpus,
        vectors=encoder.encode([corpus.text(i) for i in corpus.ids]),
        encoder=encoder,
        scorer=FakeScorer(),
    )
    with pytest.raises(RetrievalError, match="citation graph"):
        engine.search_response(SearchRequest(query="salary", mode="citation_reranked"))

"""Citation graph API tests use only a temporary local graph."""

from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from lextrace.api.app import create_app
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
from lextrace.retrieval.documents import Corpus
from lextrace.retrieval.engine import LexTraceRetriever
from lextrace.retrieval.models import Vector, unit

Sample = tuple[list[Case], BenchmarkInputs, BenchmarkConfig]


def bundle(cases: list[Case]) -> GraphEvidenceBundle:
    return GraphEvidenceBundle(
        citations=[
            CitationEvidence(
                citing_opinion_id=cases[0].opinions[0].source_id,
                cited_opinion_ids=["700"],
                relation_source="opinions-cited",
                payload_sha256=digest("api"),
                depths={"700": 1},
                complete=True,
            )
        ],
        opinion_mappings=[
            OpinionMapping(
                opinion_id="700",
                case_id=cases[1].source_id,
                date_filed=cases[1].date_filed,
                court=cases[1].court_id,
                case_name=cases[1].name,
                payload_sha256=digest("700"),
            )
        ],
        source_provenance="offline-api-test",
    )


def test_citation_endpoint_and_mode(
    benchmark_sample: Sample, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cases, _, _ = benchmark_sample
    cases = cases[:3]
    corpus_path, source = Path("cases.jsonl"), Path("source.json")
    corpus_path.write_text(serialize_cases(cases))
    source.write_text(bundle(cases).model_dump_json())
    graph_path = Path("artifacts/graphs/test")
    build_graph(corpus_path, source, graph_path)

    class Encoder:
        def encode(self, texts: Sequence[str]) -> Vector:
            return unit(np.ones((len(texts), 3), dtype=np.float32))

    class Scorer:
        def score(self, pairs: Sequence[tuple[str, str]]) -> list[float]:
            return [1.0] * len(pairs)

    corpus = Corpus(cases)
    encoder = Encoder()
    engine = LexTraceRetriever(
        corpus,
        vectors=encoder.encode(corpus.ids),
        encoder=encoder,
        scorer=Scorer(),
        graph=CitationGraph(graph_path),
    )
    with TestClient(create_app(engine)) as client:
        response = client.get(f"/cases/{cases[0].source_id}/citations")
        assert response.status_code == 200
        assert response.json()[0]["node"]["case_id"] == cases[1].source_id
        assert client.get("/cases/999/citations").status_code == 404
        assert (
            client.get(
                f"/cases/{cases[0].source_id}/citations?direction=bad"
            ).status_code
            == 422
        )
        assert (
            client.get(
                f"/cases/{cases[0].source_id}/citations?as_of_date=bad"
            ).status_code
            == 422
        )
        search = client.post(
            "/search",
            json={"query": "synthetic", "mode": "citation_reranked", "top_k": 2},
        )
        assert search.status_code == 200 and search.json()["trace"]["graph_id"]

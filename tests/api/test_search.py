"""API tests inject a local engine and never load ML models."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lextrace.api.app import create_app
from lextrace.domain.case import Case
from lextrace.evaluation.benchmark import BenchmarkConfig, BenchmarkInputs
from lextrace.retrieval.documents import Corpus
from lextrace.retrieval.engine import LexTraceRetriever

Sample = tuple[list[Case], BenchmarkInputs, BenchmarkConfig]


def test_search_and_case(benchmark_sample: Sample) -> None:
    cases, _, _ = benchmark_sample
    with TestClient(create_app(LexTraceRetriever(Corpus(cases[:4])))) as client:
        response = client.post(
            "/search",
            json={
                "query": "synthetic",
                "mode": "bm25",
                "top_k": 2,
                "filters": {"courts": ["ca2"]},
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["results"]) == 2 and data["trace"]["mode"] == "bm25"
        assert (
            client.get("/cases/1").json()["opinions"][0]["text"]
            == cases[0].opinions[0].text
        )
        assert client.get("/cases/99999").status_code == 404
        assert (
            client.post("/search", json={"query": "x", "mode": "dense"}).status_code
            == 503
        )
        assert client.get("/health").json() == {"status": "ok"}


@pytest.mark.parametrize(
    "payload",
    [
        {"query": "x", "mode": "wrong"},
        {"query": "x", "top_k": 0},
        {"query": ""},
        {
            "query": "x",
            "filters": {"filed_after": "2020-01-01", "filed_before": "2000-01-01"},
        },
    ],
)
def test_invalid_request(payload: dict[str, object]) -> None:
    with TestClient(create_app(index_path=Path("missing-index"))) as client:
        assert client.post("/search", json=payload).status_code == 422


def test_missing_index_health_independent() -> None:
    with TestClient(create_app(index_path=Path("missing-index"))) as client:
        assert client.get("/health").status_code == 200
        assert (
            client.post("/search", json={"query": "x", "mode": "bm25"}).status_code
            == 503
        )


def test_lazy_index_is_loaded_once(
    benchmark_sample: Sample, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases, _, _ = benchmark_sample
    engine = LexTraceRetriever(Corpus(cases[:2]))
    calls = 0

    def load(path: Path, **kwargs: object) -> LexTraceRetriever:
        nonlocal calls
        calls += 1
        return engine

    monkeypatch.setattr(LexTraceRetriever, "from_index", load)
    with TestClient(create_app(index_path=Path("index"))) as client:
        assert client.get("/health").status_code == 200
        assert calls == 0
        for _ in range(2):
            assert (
                client.post("/search", json={"query": "x", "mode": "bm25"}).status_code
                == 200
            )
        assert calls == 1

"""Synthetic correctness evaluation is not a real retrieval-quality experiment."""

from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest

from lextrace.domain.case import Case
from lextrace.evaluation.benchmark import BenchmarkConfig, BenchmarkInputs
from lextrace.evaluation.benchmark_build import assemble
from lextrace.evaluation.engine_run import evaluate_engine
from lextrace.retrieval.documents import Corpus
from lextrace.retrieval.engine import LexTraceRetriever
from lextrace.retrieval.models import Vector, unit

Sample = tuple[list[Case], BenchmarkInputs, BenchmarkConfig]


def test_all_modes_correctness(
    benchmark_sample: Sample, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cases, inputs, config = benchmark_sample
    selections = [
        s.model_copy(
            update={
                "review_status": "review_required",
                "leakage_reviewer": None,
                "leakage_review_approved": False,
                "known_alias_review_complete": False,
                "audit_reviewer": None,
                "positive_audits": [],
            }
        )
        for s in inputs.selections
    ]
    inputs = inputs.model_copy(update={"selections": selections})
    bundle = Path("data/benchmarks/v1/synthetic")
    bundle.mkdir(parents=True)
    for name, content in assemble(cases, inputs, config).items():
        (bundle / name).write_text(content)
    corpus = Corpus.load(bundle / "candidates.jsonl")

    class Encoder:
        def encode(self, texts: Sequence[str]) -> Vector:
            return unit(np.ones((len(texts), 3), dtype=np.float32))

    class Scorer:
        def score(self, pairs: Sequence[tuple[str, str]]) -> list[float]:
            return [1.0] * len(pairs)

    encoder = Encoder()
    engine = LexTraceRetriever(
        corpus, vectors=encoder.encode(corpus.ids), encoder=encoder, scorer=Scorer()
    )
    result = evaluate_engine(
        bundle, Path("unused"), Path("artifacts/synthetic"), engine=engine
    )
    assert result["status"] == "PROVISIONAL" and result["query_count"] == 25
    modes = result["modes"]
    assert isinstance(modes, dict)
    assert set(modes) == {"bm25", "dense", "hybrid", "reranked"}
    assert (Path("artifacts/synthetic") / "rankings.jsonl").exists()

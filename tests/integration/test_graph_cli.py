"""Offline graph CLI and citation-aware search tests."""

import json
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest

from lextrace.cli import main
from lextrace.corpus import serialize_cases
from lextrace.domain.case import Case
from lextrace.evaluation.benchmark import (
    BenchmarkConfig,
    BenchmarkInputs,
    CitationEvidence,
    OpinionMapping,
    digest,
)
from lextrace.graph.contracts import GraphEvidenceBundle
from lextrace.graph.store import CitationGraph
from lextrace.retrieval.documents import Corpus
from lextrace.retrieval.engine import LexTraceRetriever
from lextrace.retrieval.models import Vector, unit
from lextrace.retrieval.settings import EngineConfig

Sample = tuple[list[Case], BenchmarkInputs, BenchmarkConfig]


def bundle(cases: list[Case]) -> GraphEvidenceBundle:
    return GraphEvidenceBundle(
        citations=[
            CitationEvidence(
                citing_opinion_id=cases[0].opinions[0].source_id,
                cited_opinion_ids=["700"],
                relation_source="opinions-cited",
                payload_sha256=digest("graph-cli"),
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
                reporter_citations=cases[1].reporter_citations or [],
                payload_sha256=digest("700"),
            )
        ],
        source_provenance="offline-cli-test",
    )


def test_graph_build_info_citations_and_search(
    benchmark_sample: Sample,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    cases, _, _ = benchmark_sample
    cases = cases[:3]
    corpus_path, source = Path("cases.jsonl"), Path("citations.json")
    corpus_path.write_text(serialize_cases(cases))
    source.write_text(bundle(cases).model_dump_json())
    graph_path = "artifacts/graphs/test"
    main(
        [
            "build-citation-graph",
            "--corpus",
            str(corpus_path),
            "--citation-source",
            str(source),
            "--output",
            graph_path,
        ]
    )
    assert json.loads(capsys.readouterr().out)["statistics"]["edge_count"] == 1
    main(["graph-info", graph_path])
    assert (
        json.loads(capsys.readouterr().out)["source_provenance"] == "offline-cli-test"
    )
    main(["citations", cases[0].source_id, "--graph", graph_path, "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["node"]["case_id"] == cases[1].source_id

    class Encoder:
        def encode(self, texts: Sequence[str]) -> Vector:
            return unit(np.ones((len(texts), 3), dtype=np.float32))

    class Scorer:
        def score(self, pairs: Sequence[tuple[str, str]]) -> list[float]:
            return [float("target" in text) for _, text in pairs]

    corpus = Corpus(cases)
    encoder = Encoder()
    config = EngineConfig()
    config.graph.seed_count = 1
    engine = LexTraceRetriever(
        corpus,
        config,
        vectors=encoder.encode(corpus.ids),
        encoder=encoder,
        scorer=Scorer(),
        graph=CitationGraph(Path(graph_path)),
    )

    def from_index(*args: object, **kwargs: object) -> LexTraceRetriever:
        return engine

    monkeypatch.setattr(LexTraceRetriever, "from_index", from_index)
    main(
        [
            "search",
            "synthetic",
            "--mode",
            "citation_reranked",
            "--graph",
            graph_path,
            "--json",
        ]
    )
    assert json.loads(capsys.readouterr().out)["trace"]["graph_id"]


@pytest.mark.parametrize(
    "args", [["--direction", "wrong"], ["--as-of-date", "not-a-date"]]
)
def test_citation_usage_errors(args: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["citations", "1", *args])
    assert error.value.code == 2

"""Offline case-level citation normalization and SQLite traversal tests."""

from pathlib import Path

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
from lextrace.graph.contracts import GraphError, GraphEvidenceBundle
from lextrace.graph.normalize import normalize_edges
from lextrace.graph.store import CitationGraph

Sample = tuple[list[Case], BenchmarkInputs, BenchmarkConfig]


def evidence(citing: str, cited: list[str]) -> CitationEvidence:
    return CitationEvidence(
        citing_opinion_id=citing,
        cited_opinion_ids=cited,
        relation_source="opinions-cited",
        payload_sha256=digest(citing + ":" + ",".join(cited)),
        depths={identifier: index + 1 for index, identifier in enumerate(cited)},
        complete=True,
    )


def mapping(opinion: str, case: Case | None) -> OpinionMapping:
    return OpinionMapping(
        opinion_id=opinion,
        case_id=case.source_id if case else None,
        date_filed=case.date_filed if case else None,
        court=case.court_id if case else None,
        case_name=case.name if case else None,
        payload_sha256=digest(opinion),
        unresolved_reason=None if case else "unresolved",
    )


def sample(cases: list[Case]) -> tuple[list[Case], GraphEvidenceBundle]:
    first = cases[0].model_copy(deep=True)
    first.opinions.append(first.opinions[0].model_copy(update={"source_id": "999"}))
    second, third = cases[1:3]
    bundle = GraphEvidenceBundle(
        citations=[
            evidence(first.opinions[0].source_id, ["700", "701", "702"]),
            evidence("999", ["700"]),
            evidence(second.opinions[0].source_id, ["703"]),
            evidence(third.opinions[0].source_id, ["704"]),
        ],
        opinion_mappings=[
            mapping("700", second),
            mapping("701", first),
            mapping("702", None),
            mapping("703", third),
            mapping("704", first),
        ],
        source_provenance="offline-test",
    )
    return [first, second, third], bundle


def test_opinion_mapping_aggregation_and_unresolved(benchmark_sample: Sample) -> None:
    cases, _, _ = benchmark_sample
    local, bundle = sample(cases)
    edges, counts = normalize_edges(local, bundle)
    edge = next(e for e in edges if e.cited_case_id == local[1].source_id)
    assert edge.citing_case_id == local[0].source_id
    assert edge.supporting_opinion_edge_count == 2
    assert {s.citing_opinion_id for s in edge.supports} == {
        local[0].opinions[0].source_id,
        "999",
    }
    assert next(e for e in edges if e.self_edge).citing_case_id == local[0].source_id
    assert counts == {
        "source_edges": 6,
        "mapped_source_edges": 5,
        "collapsed_duplicates": 1,
        "unresolved_mappings": 1,
    }


def test_store_directions_cycles_bounds_and_dates(
    benchmark_sample: Sample, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cases, _, _ = benchmark_sample
    local, bundle = sample(cases)
    corpus = Path("cases.jsonl")
    source = Path("source.json")
    corpus.write_text(serialize_cases(local))
    source.write_text(bundle.model_dump_json())
    metadata = build_graph(corpus, source, Path("artifacts/graphs/test"))
    assert metadata.statistics.edge_count == 4
    graph = CitationGraph(Path("artifacts/graphs/test"))
    assert [n.node.case_id for n in graph.get_outgoing(local[0].source_id)] == [
        local[0].source_id,
        local[1].source_id,
    ]
    assert [n.node.case_id for n in graph.get_incoming(local[0].source_id)] == [
        local[0].source_id,
        local[2].source_id,
    ]
    assert len(graph.neighbors(local[0].source_id, direction="both")) == 4
    expanded = graph.expand([local[0].source_id], direction="both", hops=2, max_nodes=2)
    assert len(expanded.neighbors) <= 2 and expanded.deduplicated_count >= 1
    cutoff = local[1].date_filed
    assert cutoff is not None
    assert all(
        n.node.date_filed is not None and n.node.date_filed <= cutoff
        for n in graph.neighbors(
            local[0].source_id, direction="both", as_of_date=cutoff
        )
    )
    graph.close()


def test_reuse_mismatch_and_corruption(
    benchmark_sample: Sample, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cases, _, _ = benchmark_sample
    local, bundle = sample(cases)
    corpus, source, output = (
        Path("cases.jsonl"),
        Path("source.json"),
        Path("artifacts/graphs/test"),
    )
    corpus.write_text(serialize_cases(local))
    source.write_text(bundle.model_dump_json())
    first = build_graph(corpus, source, output)
    assert build_graph(corpus, source, output) == first
    source.write_text(
        bundle.model_copy(update={"source_provenance": "changed"}).model_dump_json()
    )
    with pytest.raises(GraphError, match="differ"):
        build_graph(corpus, source, output)
    with (output / "graph.sqlite3").open("ab") as handle:
        handle.write(b"broken")
    with pytest.raises(GraphError, match="checksum"):
        CitationGraph(output)

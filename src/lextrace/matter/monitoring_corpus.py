"""Immutable corpus snapshots, deterministic deltas, and bounded local publishing."""

import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from lextrace.corpus import CorpusError, atomic_write, serialize_cases
from lextrace.domain.case import Case
from lextrace.graph.contracts import GraphEvidenceBundle
from lextrace.graph.store import CitationGraph
from lextrace.matter.monitoring_contracts import CorpusDelta, CorpusVersion
from lextrace.retrieval.documents import Corpus


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def corpus_version(
    corpus: Corpus,
    *,
    corpus_id: str = "lextrace-local",
    index_identity: str | None = None,
    graph_identity: str | None = None,
    built_at: datetime | None = None,
) -> CorpusVersion:
    metadata = {}
    text = {}
    for case in corpus.cases:
        metadata[case.source_id] = _digest(
            case.model_dump(mode="json", exclude={"opinions"})
        )
        text[case.source_id] = _digest(
            [opinion.model_dump(mode="json") for opinion in case.opinions]
        )
    return CorpusVersion(
        corpus_id=corpus_id,
        version=corpus.hash,
        document_count=len(corpus.ids),
        case_ids=list(corpus.ids),
        build_timestamp=built_at or datetime.now(UTC),
        content_hash=corpus.hash,
        index_identity=index_identity,
        graph_identity=graph_identity,
        metadata_hashes=metadata,
        text_hashes=text,
    )


def corpus_delta(
    old: CorpusVersion,
    new: CorpusVersion,
    *,
    old_edges: set[str] | None = None,
    new_edges: set[str] | None = None,
) -> CorpusDelta:
    old_ids, new_ids = set(old.case_ids), set(new.case_ids)

    def ordered(ids: Iterable[str]) -> list[str]:
        return sorted(ids, key=int)

    shared = old_ids & new_ids
    return CorpusDelta(
        old_version=old.version,
        new_version=new.version,
        added_case_ids=ordered(new_ids - old_ids),
        removed_case_ids=ordered(old_ids - new_ids),
        metadata_changed_case_ids=ordered(
            i for i in shared if old.metadata_hashes[i] != new.metadata_hashes[i]
        ),
        text_changed_case_ids=ordered(
            i for i in shared if old.text_hashes[i] != new.text_hashes[i]
        ),
        citation_edges_added=sorted((new_edges or set()) - (old_edges or set())),
        citation_edges_removed=sorted((old_edges or set()) - (new_edges or set())),
    )


def merge_corpus(existing: Corpus, batch: list[Case]) -> tuple[Corpus, CorpusDelta]:
    """Merge by source ID; changed records are versioned, never counted as added."""
    by_id = dict(existing.by_id)
    for case in batch:
        by_id[case.source_id] = case
    merged = Corpus(list(by_id.values()))
    return merged, corpus_delta(corpus_version(existing), corpus_version(merged))


def graph_edge_keys(graph: CitationGraph | None, case_ids: Iterable[str]) -> set[str]:
    """Compare bounded outgoing relations for locally known cases."""
    if graph is None:
        return set()
    return {
        f"{neighbor.edge.citing_case_id}:{neighbor.edge.cited_case_id}:"
        f"{support.provenance_id}"
        for case_id in case_ids
        for neighbor in graph.get_outgoing(case_id, limit=100)
        for support in neighbor.edge.supports
    }


def graph_incoming_edge_keys(
    graph: CitationGraph | None, watched_case_ids: Iterable[str]
) -> set[str]:
    """Capture citation evidence entering watched authorities with source hashes."""
    if graph is None:
        return set()
    return {
        f"{neighbor.edge.citing_case_id}:{neighbor.edge.cited_case_id}:"
        f"{support.provenance_id}"
        for case_id in watched_case_ids
        for neighbor in graph.get_incoming(case_id, limit=100)
        for support in neighbor.edge.supports
    }


def publish_corpus(corpus: Corpus, output: Path) -> None:
    """Write a new immutable snapshot under ignored data/; index rebuild is separate."""
    if output.suffix != ".jsonl" or not output.resolve().is_relative_to(
        Path("data").resolve()
    ):
        raise CorpusError("Monitoring corpus snapshots belong under data/.")
    if output.exists():
        try:
            previous = output.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise CorpusError("Could not read existing corpus snapshot.") from None
        if previous != serialize_cases(corpus.cases):
            raise CorpusError("Existing corpus snapshot differs.")
        return
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise CorpusError("Could not create corpus snapshot directory.") from None
    atomic_write(output, serialize_cases(corpus.cases))


def merge_graph_evidence(
    old: GraphEvidenceBundle, new: GraphEvidenceBundle
) -> GraphEvidenceBundle:
    """Preserve raw opinion relations, add new evidence, and retain unresolved maps."""
    citations = {row.citing_opinion_id: row for row in old.citations}
    citations.update({row.citing_opinion_id: row for row in new.citations})
    mappings = {row.opinion_id: row for row in old.opinion_mappings}
    mappings.update({row.opinion_id: row for row in new.opinion_mappings})
    return GraphEvidenceBundle(
        citations=[citations[key] for key in sorted(citations, key=int)],
        opinion_mappings=[mappings[key] for key in sorted(mappings, key=int)],
        source_provenance=f"{old.source_provenance}; {new.source_provenance}",
    )

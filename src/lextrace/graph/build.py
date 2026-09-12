"""Atomic construction of a versioned local SQLite citation graph."""

import json
import sqlite3
import tempfile
from pathlib import Path

from pydantic import ValidationError

from lextrace.corpus import read_cases
from lextrace.evaluation.benchmark import canonical, digest
from lextrace.graph.contracts import (
    GraphError,
    GraphEvidenceBundle,
    GraphNode,
    GraphStatistics,
)
from lextrace.graph.normalize import normalize_edges
from lextrace.graph.store import CitationGraph, GraphMetadata, _hash, degree_statistics


def build_graph(corpus_path: Path, source_path: Path, output: Path) -> GraphMetadata:
    if not output.resolve().is_relative_to(Path("artifacts/graphs").resolve()):
        raise GraphError("Citation graphs must be stored under artifacts/graphs/.")
    try:
        corpus_bytes = corpus_path.read_bytes()
        source_bytes = source_path.read_bytes()
        cases = read_cases(corpus_path)
        bundle = GraphEvidenceBundle.model_validate_json(source_bytes)
    except (OSError, ValidationError, ValueError):
        raise GraphError("Could not read valid graph build inputs.") from None
    if output.exists():
        graph = CitationGraph(output)
        try:
            if graph.metadata.source_corpus_sha256 != digest(
                corpus_bytes
            ) or graph.metadata.source_evidence_sha256 != digest(source_bytes):
                raise GraphError("Existing citation graph inputs differ.")
            return graph.metadata
        finally:
            graph.close()
    edges, counts = normalize_edges(cases, bundle)
    local = {case.source_id: case for case in cases}
    mapped = {m.case_id: m for m in bundle.opinion_mappings if m.case_id is not None}
    node_ids = sorted(
        set(local)
        | {edge.citing_case_id for edge in edges}
        | {edge.cited_case_id for edge in edges},
        key=int,
    )
    nodes = []
    for identifier in node_ids:
        case = local.get(identifier)
        mapping = mapped.get(identifier)
        nodes.append(
            GraphNode(
                case_id=identifier,
                case_name=case.name if case else mapping.case_name if mapping else None,
                court=case.court_id if case else mapping.court if mapping else None,
                date_filed=case.date_filed
                if case
                else mapping.date_filed
                if mapping
                else None,
                reporter_citations=case.reporter_citations
                if case
                else mapping.reporter_citations
                if mapping
                else None,
                source_url=str(case.source_url) if case else None,
                searchable=case is not None,
            )
        )
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            dir=output.parent, prefix=".graph-"
        ) as temporary:
            folder = Path(temporary) / "graph"
            folder.mkdir()
            database = folder / "graph.sqlite3"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                PRAGMA journal_mode=DELETE;
                CREATE TABLE graph_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE nodes(
                    case_id TEXT PRIMARY KEY, case_name TEXT, court TEXT,
                    date_filed TEXT, reporter_citations TEXT, source_url TEXT,
                    searchable INTEGER NOT NULL
                );
                CREATE TABLE edges(
                    citing_case_id TEXT NOT NULL, cited_case_id TEXT NOT NULL,
                    citing_case_date TEXT, cited_case_date TEXT,
                    support_count INTEGER NOT NULL, minimum_depth INTEGER,
                    maximum_depth INTEGER, self_edge INTEGER NOT NULL,
                    PRIMARY KEY(citing_case_id,cited_case_id)
                );
                CREATE TABLE supports(
                    citing_case_id TEXT NOT NULL, cited_case_id TEXT NOT NULL,
                    citing_opinion_id TEXT NOT NULL, cited_opinion_id TEXT NOT NULL,
                    provenance_id TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(citing_case_id,cited_case_id,provenance_id)
                );
                CREATE INDEX edges_incoming ON edges(cited_case_id,citing_case_id);
                CREATE INDEX edges_outgoing ON edges(citing_case_id,cited_case_id);
                """
            )
            connection.execute(
                "INSERT INTO graph_metadata VALUES('format_version','1')"
            )
            connection.executemany(
                "INSERT INTO nodes VALUES(?,?,?,?,?,?,?)",
                [
                    (
                        n.case_id,
                        n.case_name,
                        n.court,
                        n.date_filed.isoformat() if n.date_filed else None,
                        json.dumps(n.reporter_citations)
                        if n.reporter_citations is not None
                        else None,
                        n.source_url,
                        int(n.searchable),
                    )
                    for n in nodes
                ],
            )
            for edge in edges:
                connection.execute(
                    "INSERT INTO edges VALUES(?,?,?,?,?,?,?,?)",
                    (
                        edge.citing_case_id,
                        edge.cited_case_id,
                        edge.citing_case_date.isoformat()
                        if edge.citing_case_date
                        else None,
                        edge.cited_case_date.isoformat()
                        if edge.cited_case_date
                        else None,
                        edge.supporting_opinion_edge_count,
                        edge.minimum_citation_depth,
                        edge.maximum_citation_depth,
                        int(edge.self_edge),
                    ),
                )
                connection.executemany(
                    "INSERT INTO supports VALUES(?,?,?,?,?,?)",
                    [
                        (
                            edge.citing_case_id,
                            edge.cited_case_id,
                            s.citing_opinion_id,
                            s.cited_opinion_id,
                            s.provenance_id,
                            canonical(s),
                        )
                        for s in edge.supports
                    ],
                )
            connection.commit()
            indegrees = [
                row[0]
                for row in connection.execute(
                    "SELECT count(*) FROM edges GROUP BY cited_case_id"
                )
            ]
            outdegrees = [
                row[0]
                for row in connection.execute(
                    "SELECT count(*) FROM edges GROUP BY citing_case_id"
                )
            ]
            connection.close()
            imed, ip95 = degree_statistics(indegrees)
            omed, op95 = degree_statistics(outdegrees)
            stats = GraphStatistics(
                node_count=len(nodes),
                edge_count=len(edges),
                unique_citing_cases=len(outdegrees),
                unique_cited_cases=len(indegrees),
                self_edge_count=sum(e.self_edge for e in edges),
                source_edges=counts["source_edges"],
                mapped_source_edges=counts["mapped_source_edges"],
                collapsed_duplicates=counts["collapsed_duplicates"],
                unresolved_mappings=counts["unresolved_mappings"],
                corpus_node_coverage=sum(n.searchable for n in nodes) / len(cases),
                corpus_with_outgoing=sum(
                    1 for i in local if any(e.citing_case_id == i for e in edges)
                ),
                corpus_with_incoming=sum(
                    1 for i in local if any(e.cited_case_id == i for e in edges)
                ),
                in_degree_median=imed,
                in_degree_p95=ip95,
                out_degree_median=omed,
                out_degree_p95=op95,
            )
            metadata = GraphMetadata(
                graph_id=digest(digest(corpus_bytes) + digest(source_bytes))[:24],
                source_corpus_sha256=digest(corpus_bytes),
                source_evidence_sha256=digest(source_bytes),
                database_sha256=_hash(database),
                source_provenance=bundle.source_provenance,
                statistics=stats,
            )
            (folder / "metadata.json").write_text(
                metadata.model_dump_json(indent=2) + "\n"
            )
            folder.rename(output)
            return metadata
    except GraphError:
        raise
    except (OSError, sqlite3.Error):
        raise GraphError(
            "Citation graph build failed; no partial graph was published."
        ) from None

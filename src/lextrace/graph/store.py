"""Read-only indexed SQLite citation graph service."""

import hashlib
import json
import math
import sqlite3
from datetime import date
from pathlib import Path
from statistics import median
from typing import Literal

from pydantic import ValidationError

from lextrace.graph.contracts import (
    CitationEdge,
    CitationNeighbor,
    Direction,
    ExpansionResult,
    GraphError,
    GraphNode,
    GraphStatistics,
    OpinionCitation,
)
from lextrace.retrieval.contracts import Record


class GraphMetadata(Record):
    format_version: Literal[1] = 1
    graph_id: str
    source_corpus_sha256: str
    source_evidence_sha256: str
    database_sha256: str
    source_provenance: str
    statistics: GraphStatistics


def _hash(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


class CitationGraph:
    def __init__(self, path: Path) -> None:
        try:
            self.path = path
            self.metadata = GraphMetadata.model_validate_json(
                (path / "metadata.json").read_bytes()
            )
            database = path / "graph.sqlite3"
            if _hash(database) != self.metadata.database_sha256:
                raise GraphError("Citation graph checksum mismatch.")
            self._connection = sqlite3.connect(
                f"file:{database}?mode=ro&immutable=1",
                uri=True,
                check_same_thread=False,
            )
            self._connection.row_factory = sqlite3.Row
            if self._connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise GraphError("Citation graph database is corrupt.")
            version = self._connection.execute(
                "SELECT value FROM graph_metadata WHERE key='format_version'"
            ).fetchone()
            if version is None or version[0] != "1":
                raise GraphError("Unsupported citation graph format.")
        except GraphError:
            raise
        except (OSError, sqlite3.Error, ValidationError, TypeError):
            raise GraphError("Could not open a valid citation graph.") from None

    def close(self) -> None:
        self._connection.close()

    def get_node(self, case_id: str) -> GraphNode | None:
        row = self._connection.execute(
            "SELECT * FROM nodes WHERE case_id=?", (case_id,)
        ).fetchone()
        return None if row is None else self._node(row)

    def _node(self, row: sqlite3.Row) -> GraphNode:
        return GraphNode(
            case_id=row["case_id"],
            case_name=row["case_name"],
            court=row["court"],
            date_filed=row["date_filed"],
            reporter_citations=json.loads(row["reporter_citations"])
            if row["reporter_citations"] is not None
            else None,
            source_url=row["source_url"],
            searchable=bool(row["searchable"]),
        )

    def _edge(self, row: sqlite3.Row) -> CitationEdge:
        supports = [
            OpinionCitation.model_validate_json(item[0])
            for item in self._connection.execute(
                "SELECT payload FROM supports "
                "WHERE citing_case_id=? AND cited_case_id=? "
                "ORDER BY citing_opinion_id+0,cited_opinion_id+0,provenance_id",
                (row["citing_case_id"], row["cited_case_id"]),
            )
        ]
        return CitationEdge(
            citing_case_id=row["citing_case_id"],
            cited_case_id=row["cited_case_id"],
            citing_case_date=row["citing_case_date"],
            cited_case_date=row["cited_case_date"],
            supports=supports,
            supporting_opinion_edge_count=row["support_count"],
            minimum_citation_depth=row["minimum_depth"],
            maximum_citation_depth=row["maximum_depth"],
            self_edge=bool(row["self_edge"]),
        )

    def neighbors(
        self,
        case_id: str,
        *,
        direction: Direction = "outgoing",
        limit: int = 100,
        as_of_date: date | None = None,
    ) -> list[CitationNeighbor]:
        if limit < 1 or limit > 10_000:
            raise GraphError("Citation neighbor limit must be between 1 and 10000.")
        directions: tuple[Literal["outgoing", "incoming"], ...] = (
            ("outgoing", "incoming") if direction == "both" else (direction,)
        )
        found: dict[tuple[str, str, str], CitationNeighbor] = {}
        for selected in directions:
            if selected == "outgoing":
                where, join, node_id = (
                    "e.citing_case_id=?",
                    "e.cited_case_id",
                    "e.cited_case_id",
                )
            else:
                where, join, node_id = (
                    "e.cited_case_id=?",
                    "e.citing_case_id",
                    "e.citing_case_id",
                )
            sql = (
                "SELECT e.*,n.* FROM edges e JOIN nodes n ON n.case_id="
                + join
                + " WHERE "
                + where
                + " ORDER BY "
                + node_id
                + "+0 LIMIT ?"
            )
            for row in self._connection.execute(sql, (case_id, limit)):
                node = self._node(row)
                if as_of_date is not None and (
                    node.date_filed is None or node.date_filed > as_of_date
                ):
                    continue
                edge = self._edge(row)
                found[(node.case_id, selected, edge.citing_case_id)] = CitationNeighbor(
                    node=node, edge=edge, direction=selected
                )
        return sorted(found.values(), key=lambda n: (int(n.node.case_id), n.direction))[
            :limit
        ]

    def get_outgoing(self, case_id: str, *, limit: int = 100) -> list[CitationNeighbor]:
        return self.neighbors(case_id, direction="outgoing", limit=limit)

    def get_incoming(self, case_id: str, *, limit: int = 100) -> list[CitationNeighbor]:
        return self.neighbors(case_id, direction="incoming", limit=limit)

    def expand(
        self,
        seed_case_ids: list[str],
        *,
        hops: int = 1,
        direction: Direction = "outgoing",
        max_nodes: int = 100,
        as_of_date: date | None = None,
    ) -> ExpansionResult:
        if hops not in (1, 2) or not 1 <= max_nodes <= 10_000:
            raise GraphError(
                "Expansion requires one or two hops and a bounded node cap."
            )
        seeds = list(dict.fromkeys(seed_case_ids))
        visited = set(seeds)
        frontier = [(seed, seed) for seed in seeds]
        results: list[CitationNeighbor] = []
        duplicates = 0
        for hop in range(1, hops + 1):
            following: list[tuple[str, str]] = []
            for current, origin in frontier:
                for neighbor in self.neighbors(
                    current,
                    direction=direction,
                    limit=max_nodes,
                    as_of_date=as_of_date,
                ):
                    if neighbor.node.case_id in visited:
                        duplicates += 1
                        continue
                    visited.add(neighbor.node.case_id)
                    following.append((neighbor.node.case_id, origin))
                    results.append(
                        neighbor.model_copy(update={"hop": hop, "seed_case_id": origin})
                    )
                    if len(results) >= max_nodes:
                        return ExpansionResult(
                            seed_case_ids=seeds,
                            neighbors=results,
                            discovered_count=len(results),
                            deduplicated_count=duplicates,
                        )
            frontier = sorted(following, key=lambda item: (int(item[0]), int(item[1])))
        return ExpansionResult(
            seed_case_ids=seeds,
            neighbors=results,
            discovered_count=len(results),
            deduplicated_count=duplicates,
        )


def degree_statistics(values: list[int]) -> tuple[float, int]:
    if not values:
        return 0.0, 0
    ordered = sorted(values)
    return median(ordered), ordered[math.ceil(0.95 * len(ordered)) - 1]

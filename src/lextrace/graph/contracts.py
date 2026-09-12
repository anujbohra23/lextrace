"""Typed case-level citation graph contracts."""

from datetime import date
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from lextrace.evaluation.benchmark import CitationEvidence, OpinionMapping
from lextrace.retrieval.contracts import Record

GraphId = Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]*$")]
Direction = Literal["outgoing", "incoming", "both"]


class GraphError(Exception):
    """Safe graph validation or storage error."""


class OpinionCitation(Record):
    citing_opinion_id: GraphId
    cited_opinion_id: GraphId
    citation_depth: Annotated[int, Field(strict=True, ge=0)] | None = None
    provenance_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class CitationEdge(Record):
    citing_case_id: GraphId
    cited_case_id: GraphId
    citing_case_date: date | None = None
    cited_case_date: date | None = None
    source: Literal["courtlistener"] = "courtlistener"
    supports: Annotated[list[OpinionCitation], Field(min_length=1)]
    supporting_opinion_edge_count: Annotated[int, Field(strict=True, gt=0)]
    minimum_citation_depth: Annotated[int, Field(strict=True, ge=0)] | None = None
    maximum_citation_depth: Annotated[int, Field(strict=True, ge=0)] | None = None
    self_edge: bool = False

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.supporting_opinion_edge_count != len(self.supports):
            raise ValueError("Support count does not match citation provenance.")
        if self.self_edge != (self.citing_case_id == self.cited_case_id):
            raise ValueError("Self-edge marker is inconsistent.")
        return self


class GraphNode(Record):
    case_id: GraphId
    case_name: str | None = None
    court: str | None = None
    date_filed: date | None = None
    reporter_citations: list[str] | None = None
    source_url: str | None = None
    searchable: bool = False


class CitationNeighbor(Record):
    node: GraphNode
    edge: CitationEdge
    direction: Literal["outgoing", "incoming"]
    hop: Annotated[int, Field(strict=True, gt=0)] = 1


class ExpansionResult(Record):
    seed_case_ids: list[GraphId]
    neighbors: list[CitationNeighbor]
    discovered_count: int
    deduplicated_count: int


class GraphStatistics(Record):
    node_count: int
    edge_count: int
    unique_citing_cases: int
    unique_cited_cases: int
    self_edge_count: int
    source_edges: int
    mapped_source_edges: int
    collapsed_duplicates: int
    unresolved_mappings: int
    corpus_node_coverage: float
    corpus_with_outgoing: int
    corpus_with_incoming: int
    in_degree_median: float
    in_degree_p95: int
    out_degree_median: float
    out_degree_p95: int


class GraphEvidenceBundle(Record):
    citations: list[CitationEvidence]
    opinion_mappings: list[OpinionMapping]
    source_provenance: str

"""Public retrieval contracts, independent of acquisition and benchmark labels."""

from datetime import date
from typing import Annotated, Literal, Protocol, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    model_validator,
)

Mode = Literal["bm25", "dense", "hybrid", "reranked", "citation_reranked"]
Positive = Annotated[int, Field(strict=True, gt=0)]
Finite = Annotated[float, Field(allow_inf_nan=False)]
Nonempty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class RetrievalError(Exception):
    """Safe operational error; never includes model exception text or user queries."""


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchFilters(Record):
    courts: list[Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+$")]] = Field(
        default_factory=list
    )
    filed_after: date | None = None
    filed_before: date | None = None

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if (
            self.filed_after
            and self.filed_before
            and self.filed_after > self.filed_before
        ):
            raise ValueError("Invalid filing-date range.")
        return self


class Passage(Record):
    passage_id: str
    opinion_id: str
    start: Annotated[int, Field(ge=0)]
    end: Positive
    text: str
    score: Finite = 0
    scoring_method: Literal["lexical-evidence", "cross-encoder"] = "lexical-evidence"


class Diagnostics(Record):
    bm25_score: Finite | None = None
    bm25_rank: Positive | None = None
    dense_score: Finite | None = None
    dense_rank: Positive | None = None
    hybrid_score: Finite | None = None
    reranker_score: Finite | None = None
    discovered_via_citation: bool = False
    citation_provenance: list["CitationProvenance"] = Field(default_factory=list)


class CitationProvenance(Record):
    seed_case_id: str
    candidate_case_id: str
    direction: Literal["outgoing", "incoming"]
    hop: Positive
    citing_case_id: str
    cited_case_id: str
    supporting_opinion_edge_count: Positive
    provenance_ids: list[str]


class RetrievalResult(Record):
    case_id: str
    rank: Positive
    final_score: Finite
    retrieval_method: Mode
    case_name: str
    court: str
    date_filed: date | None
    reporter_citations: list[str] | None
    source_url: HttpUrl
    relevant_passage: Passage
    diagnostics: Diagnostics


class RetrievalTrace(Record):
    request_id: str
    mode: Mode
    corpus_hash: str
    index_version: str
    query_length: int
    candidate_counts: dict[str, int] = Field(default_factory=dict)
    stage_seconds: dict[str, float] = Field(default_factory=dict)
    embedding_model: str | None = None
    reranker_model: str | None = None
    graph_id: str | None = None


class SearchResponse(Record):
    results: list[RetrievalResult]
    trace: RetrievalTrace


class SearchRequest(Record):
    query: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
    ]
    top_k: Annotated[int, Field(strict=True, ge=1, le=100)] = 10
    mode: Mode = "reranked"
    filters: SearchFilters | None = None


class Retriever(Protocol):
    def search(
        self, query: str, top_k: int = 10, filters: SearchFilters | None = None
    ) -> list[RetrievalResult]: ...

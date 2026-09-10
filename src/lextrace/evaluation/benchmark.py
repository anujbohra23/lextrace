"""Typed, versioned records for the V1 Citation-Recovery Benchmark."""

import hashlib
import json
from datetime import date
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

SourceId = Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]*$")]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class BenchmarkError(Exception):
    """A safe benchmark validation error; never includes source text."""


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BenchmarkConfig(Record):
    format_version: Literal[1] = 1
    benchmark_id: Literal["citation-recovery-v1"] = "citation-recovery-v1"
    task: Literal["Citation-Recovery Benchmark"] = "Citation-Recovery Benchmark"
    generation_rules: Literal["citation-recovery-v1.0"] = "citation-recovery-v1.0"
    normalizer_version: Literal["milestone-1"] = "milestone-1"
    query_court: Literal["ca2"] = "ca2"
    query_date_start: date = date(2010, 1, 1)
    query_date_end: date = date(2010, 12, 31)
    candidate_cutoff: date = date(2009, 12, 31)
    candidate_courts: tuple[Literal["ca2"], Literal["scotus"]] = ("ca2", "scotus")
    query_count: Literal[25] = 25
    dev_count: Literal[10] = 10
    test_count: Literal[15] = 15
    candidate_min: Literal[200] = 200
    candidate_target: Annotated[int, Field(ge=200, le=500)] = 300
    candidate_max: Literal[500] = 500
    minimum_positives: Literal[2] = 2
    excerpt_min_words: Literal[150] = 150
    excerpt_max_words: Literal[300] = 300
    detailed_audit_queries: Literal[15] = 15
    selection_seed: int = 1729
    distractor_sampling: Literal["court-year-sha256-round-robin-v1"] = (
        "court-year-sha256-round-robin-v1"
    )

    @model_validator(mode="after")
    def fixed_dates(self) -> Self:
        if (self.query_date_start, self.query_date_end, self.candidate_cutoff) != (
            date(2010, 1, 1),
            date(2010, 12, 31),
            date(2009, 12, 31),
        ):
            raise ValueError("V1 date boundaries are fixed.")
        return self


class Span(Record):
    """Half-open Unicode character offsets in normalized source opinion text."""

    start: Annotated[int, Field(ge=0)]
    end: Annotated[int, Field(gt=0)]

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("Invalid span.")
        return self


class Removal(Span):
    reason: Literal[
        "reporter_citation", "case_reference", "short_form", "citation_bearing_clause"
    ]


class PositiveAudit(Record):
    case_id: SourceId
    context: Literal[
        "substantive", "procedural", "background", "adverse treatment", "uncertain"
    ]
    citation_context: Span
    note: Text


class QuerySelection(Record):
    query_id: Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]+$")]
    source_case_id: SourceId
    source_opinion_id: SourceId
    source_text_sha256: Digest
    excerpt: Span
    removals: list[Removal] = Field(default_factory=list)
    transformation: Literal["remove-spans-collapse-whitespace-v1"] = (
        "remove-spans-collapse-whitespace-v1"
    )
    leakage_reviewer: Text
    leakage_review_approved: Literal[True]
    known_alias_review_complete: Literal[True]
    selection_note: Text
    audit_reviewer: Text | None = None
    positive_audits: list[PositiveAudit] = Field(default_factory=list)


class CitationEvidence(Record):
    """Complete observed outgoing relations for one selected source opinion."""

    citing_opinion_id: SourceId
    cited_opinion_ids: list[SourceId]
    relation_source: Literal["opinions_cited", "opinions-cited"]
    payload_sha256: Digest
    complete: Literal[True]


class OpinionMapping(Record):
    """Frozen metadata resolving an opinion identifier to a decision cluster."""

    opinion_id: SourceId
    case_id: SourceId | None
    date_filed: date | None = None
    court: str | None = None
    case_name: str | None = None
    reporter_citations: list[str] = Field(default_factory=list)
    aliases: list[Text] = Field(default_factory=list)
    payload_sha256: Digest
    unresolved_reason: Literal["unresolved", "ambiguous", "unavailable"] | None = None

    @model_validator(mode="after")
    def resolution(self) -> Self:
        if (self.case_id is None) != (self.unresolved_reason is not None):
            raise ValueError("Mapping resolution must be explicit.")
        return self


class BenchmarkInputs(Record):
    selections: list[QuerySelection]
    citations: list[CitationEvidence]
    opinion_mappings: list[OpinionMapping]
    # Groups contain cluster IDs, including identified prior proceedings.
    litigation_groups: dict[str, list[SourceId]] = Field(default_factory=dict)
    litigation_review_note: Text
    source_provenance: Text


class BenchmarkItem(Record):
    query_id: Text
    source_case_id: SourceId
    source_opinion_id: SourceId
    query_text: Text
    query_date: date
    query_court: Literal["ca2"]
    positive_case_ids: Annotated[list[SourceId], Field(min_length=2)]
    candidate_set_id: Literal["v1-candidates"] = "v1-candidates"
    split: Literal["dev", "test"]
    query_provenance_id: Text

    @model_validator(mode="after")
    def distinct_positives(self) -> Self:
        if len(self.positive_case_ids) != len(set(self.positive_case_ids)):
            raise ValueError("Duplicate positive IDs.")
        return self


ExclusionReason = Literal[
    "self",
    "same_litigation",
    "unresolved",
    "missing_date",
    "out_of_scope_court",
    "after_cutoff",
]


class Exclusion(Record):
    query_id: Text
    cited_opinion_id: SourceId
    reason: ExclusionReason


class BenchmarkManifest(Record):
    format_version: Literal[1] = 1
    config: BenchmarkConfig
    code_sha256: Digest
    source_corpus_sha256: Digest
    provenance_sha256: Digest
    queries_sha256: Digest
    candidates_sha256: Digest
    query_case_ids: list[SourceId]
    candidate_case_ids: list[SourceId]
    positive_union_ids: list[SourceId]
    candidate_strata: dict[str, list[SourceId]]
    split_query_ids: dict[str, list[str]]
    audited_query_ids: list[str]
    leakage_reviewed_queries: int
    exclusions: list[Exclusion]
    exclusion_counts: dict[str, int]
    metric_labels: Literal[
        "binary citation-derived positives; other candidates unjudged"
    ] = "binary citation-derived positives; other candidates unjudged"


def digest(content: str | bytes) -> str:
    return hashlib.sha256(
        content.encode("utf-8") if isinstance(content, str) else content
    ).hexdigest()


def canonical(model: BaseModel) -> str:
    return json.dumps(
        model.model_dump(mode="json"),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )

"""Local private evidence retrieval; ephemeral indexes never enter public storage.

Approved versions are a caller's scope restriction, not a claim of factual truth or
review acceptance. Ownership, currency, source text and sections come from storage.
"""

import hashlib
import json
import math
from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, model_validator

from lextrace.investigations.contracts import (
    SHA256,
    DocumentVersion,
    EvidenceReference,
    Identifier,
    SourceIdentity,
)
from lextrace.investigations.provenance import validate_evidence_reference
from lextrace.investigations.repository import InvestigationRepository
from lextrace.matter.contracts import DocumentKind, MatterError
from lextrace.matter.store import MatterStore
from lextrace.retrieval.bm25_core import BM25Config, BM25Core, tokenize
from lextrace.retrieval.contracts import Record
from lextrace.retrieval.dense import dense_search
from lextrace.retrieval.fusion import rrf
from lextrace.retrieval.models import Encoder, PairScorer, unit
from lextrace.retrieval.settings import DenseSettings, LexicalSettings, RerankerSettings


class StrictRecord(Record):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ApprovedVersion(StrictRecord):
    document_id: Identifier
    version_id: Identifier


class EvidenceFilters(StrictRecord):
    document_ids: tuple[Identifier, ...] = ()
    document_types: tuple[DocumentKind, ...] = ()
    section_ids: tuple[Identifier, ...] = ()


class EvidenceSearchRequest(StrictRecord):
    matter_id: Identifier
    query: Annotated[str, Field(min_length=1, max_length=10000)]
    approved_versions: Annotated[tuple[ApprovedVersion, ...], Field(max_length=100)]
    filters: EvidenceFilters = Field(default_factory=EvidenceFilters)
    mode: Literal["bm25", "dense", "hybrid", "reranked"] = "hybrid"
    candidate_budget: Annotated[int, Field(ge=1, le=1000)] = 100
    result_budget: Annotated[int, Field(ge=1, le=100)] = 10
    expected_index_identity: SHA256 | None = None

    @model_validator(mode="after")
    def valid_budgets(self) -> Self:
        if self.result_budget > self.candidate_budget:
            raise ValueError("Result budget exceeds candidate budget.")
        ids = [v.document_id for v in self.approved_versions]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate approved document.")
        return self


class EvidenceHit(StrictRecord):
    reference: EvidenceReference
    score: Annotated[float, Field(allow_inf_nan=False)]


class EvidenceDiagnostics(StrictRecord):
    passage_count: int
    candidate_count: int
    returned_count: int
    index_storage: Literal["ephemeral"] = "ephemeral"


class EvidenceSearchResult(StrictRecord):
    matter_id: Identifier
    query: str
    approved_versions: tuple[ApprovedVersion, ...]
    index_identity: SHA256
    hits: tuple[EvidenceHit, ...]
    diagnostics: EvidenceDiagnostics


class PrivateIndexSettings(StrictRecord):
    chunk_size: Annotated[int, Field(ge=1, le=10000)] = 800
    overlap: Annotated[int, Field(ge=0)] = 100
    max_passages: Annotated[int, Field(ge=1, le=100000)] = 10000
    max_source_characters: Annotated[int, Field(ge=1)] = 10000000
    bm25: LexicalSettings = Field(default_factory=LexicalSettings)
    dense: DenseSettings = Field(default_factory=DenseSettings)
    reranker: RerankerSettings = Field(default_factory=RerankerSettings)
    pooling: Literal["normalized-window-mean-v1"] = "normalized-window-mean-v1"
    tokenizer: Literal["unicode-alnum-lower-v1"] = "unicode-alnum-lower-v1"
    preprocessing: Literal["exact-section-codepoints-v1"] = (
        "exact-section-codepoints-v1"
    )
    rrf_constant: Annotated[int, Field(ge=1)] = 60

    @model_validator(mode="after")
    def valid_overlap(self) -> Self:
        if self.overlap >= self.chunk_size:
            raise ValueError("Overlap must be smaller than chunk size.")
        return self


class EvidenceSearchService:
    """M6-facing service. Inject local models matching the server-owned settings.

    No cache or file survives a call: deletion/replacement has no private index
    artifacts to clean up. Only the selected corpus is encoded or counted by BM25.
    Model instances must not retain inputs. Diagnostics never contain source text.
    """

    def __init__(
        self,
        store: MatterStore,
        repository: InvestigationRepository,
        *,
        settings: PrivateIndexSettings | None = None,
        encoder: Encoder | None = None,
        reranker: PairScorer | None = None,
    ) -> None:
        self.store = store
        self.repository = repository
        self.settings = PrivateIndexSettings.model_validate(
            (settings or PrivateIndexSettings()).model_dump(), strict=True
        )
        self.encoder = encoder
        self.reranker = reranker

    def _version(self, matter: str, approved: ApprovedVersion) -> DocumentVersion:
        version = self.repository.active_document_version(matter, approved.document_id)
        if version is None or version.version_id != approved.version_id:
            raise MatterError("Evidence source is unavailable or stale.")
        return version

    def search(self, request: EvidenceSearchRequest) -> EvidenceSearchResult:
        try:
            return self._search(request)
        except Exception:
            # Model/storage exceptions may contain private inputs or filesystem paths.
            raise MatterError("Private evidence search failed.") from None

    def _search(self, request: EvidenceSearchRequest) -> EvidenceSearchResult:
        request = EvidenceSearchRequest.model_validate(
            request.model_dump(), strict=True
        )
        if self.store.get_matter(request.matter_id) is None:
            raise MatterError("Matter is unavailable.")
        refs: list[EvidenceReference] = []
        versions: list[DocumentVersion] = []
        characters = 0
        for approved in sorted(request.approved_versions, key=lambda v: v.document_id):
            version = self._version(request.matter_id, approved)
            versions.append(version)
            document = self.store.get_document(request.matter_id, approved.document_id)
            if document is None or document.ingestion_status != "READY":
                raise MatterError("Evidence source is unavailable.")
            filters = request.filters
            if (
                filters.document_ids and version.document_id not in filters.document_ids
            ) or (
                filters.document_types
                and document.document_type not in filters.document_types
            ):
                continue
            text = self.store.document_text(request.matter_id, version.document_id)
            if (
                hashlib.sha256(text.encode("utf-8")).hexdigest()
                != version.extracted_text_hash
                or len(text) != version.text_length
            ):
                raise MatterError("Invalid source text.")
            characters += len(text)
            if characters > self.settings.max_source_characters:
                raise MatterError("Private source budget exceeded.")
            sections = self.store.sections(request.matter_id, version.document_id)
            if text and not sections:
                raise MatterError("Source sections unavailable.")
            for section in sorted(sections, key=lambda s: (s.span.start, s.section_id)):
                source_identity = {
                    k: getattr(version, k) for k in SourceIdentity.model_fields
                }
                full = EvidenceReference(
                    **source_identity,
                    section_id=section.section_id,
                    start=section.span.start,
                    end=section.span.end,
                    exact_quote=section.text,
                )
                if validate_evidence_reference(full, version, section, text):
                    raise MatterError("Invalid source provenance.")
                if (
                    filters.section_ids
                    and section.section_id not in filters.section_ids
                ):
                    continue
                start = section.span.start
                while start < section.span.end:
                    end = min(start + self.settings.chunk_size, section.span.end)
                    quote = text[start:end]
                    reference = EvidenceReference(
                        **source_identity,
                        section_id=section.section_id,
                        start=start,
                        end=end,
                        exact_quote=quote,
                    )
                    if validate_evidence_reference(reference, version, section, text):
                        raise MatterError("Invalid source provenance.")
                    if tokenize(quote):
                        refs.append(reference)
                    if len(refs) > self.settings.max_passages:
                        raise MatterError("Private passage budget exceeded.")
                    if end == section.span.end:
                        break
                    start = end - self.settings.overlap
        refs.sort(
            key=lambda r: (r.document_id, r.version_id, r.start, r.end, r.section_id)
        )
        payload = {
            "format": 1,
            "matter": request.matter_id,
            "versions": [v.model_dump(mode="json") for v in versions],
            "settings": self.settings.model_dump(mode="json"),
            "filters": request.filters.model_dump(mode="json"),
            "passages": [r.model_dump(mode="json") for r in refs],
        }
        identity = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()
        if request.expected_index_identity not in (None, identity):
            raise MatterError("Incompatible private index identity.")
        ranked: list[tuple[str, float]] = []
        if refs and tokenize(request.query):
            ids = tuple(str(i) for i in range(len(refs)))
            texts = [r.exact_quote for r in refs]
            depth = request.candidate_budget
            lexical: list[tuple[str, float]] = []
            dense: list[tuple[str, float]] = []
            if request.mode != "dense":
                lexical = BM25Core(
                    list(zip(ids, texts, strict=True)),
                    BM25Config(self.settings.bm25.k1, self.settings.bm25.b),
                    tie_breaker=int,
                ).search(request.query, top_k=depth)
                lexical = [(i, s) for i, s in lexical if s > 0]
            if request.mode != "bm25":
                if self.encoder is None:
                    raise MatterError("Local encoder required.")
                vectors = unit(self.encoder.encode(texts))
                if vectors.shape[0] != len(ids):
                    raise MatterError("Invalid local embeddings.")
                dense = dense_search(
                    vectors,
                    ids,
                    self.encoder.encode([request.query]),
                    set(ids),
                    depth,
                    self.settings.dense.block_size,
                )
            if request.mode == "bm25":
                ranked = lexical
            elif request.mode == "dense":
                ranked = dense
            else:
                ranked = rrf(
                    [i for i, _ in lexical],
                    [i for i, _ in dense],
                    self.settings.rrf_constant,
                )[:depth]
            if request.mode == "reranked":
                if self.reranker is None:
                    raise MatterError("Local reranker required.")
                scores = self.reranker.score(
                    [(request.query, texts[int(i)]) for i, _ in ranked]
                )
                if len(scores) != len(ranked) or not all(
                    math.isfinite(s) for s in scores
                ):
                    raise MatterError("Invalid local scores.")
                ranked = sorted(
                    [(i, float(s)) for (i, _), s in zip(ranked, scores, strict=True)],
                    key=lambda row: (-row[1], int(row[0])),
                )
        hits: list[EvidenceHit] = []
        for identifier, score in ranked:
            ref = refs[int(identifier)]
            if any(
                h.reference.document_id == ref.document_id
                and h.reference.version_id == ref.version_id
                and max(h.reference.start, ref.start) < min(h.reference.end, ref.end)
                for h in hits
            ):
                continue
            hits.append(EvidenceHit(reference=ref, score=score))
            if len(hits) == request.result_budget:
                break
        # Re-resolve after model calls: replacement/deletion invalidates the call.
        for approved, version in zip(
            sorted(request.approved_versions, key=lambda v: v.document_id),
            versions,
            strict=True,
        ):
            if self._version(request.matter_id, approved) != version:
                raise MatterError("Evidence source changed.")
        for hit in hits:
            ref = hit.reference
            version = self._version(
                request.matter_id,
                ApprovedVersion(document_id=ref.document_id, version_id=ref.version_id),
            )
            text = self.store.document_text(request.matter_id, ref.document_id)
            current_section = next(
                (
                    s
                    for s in self.store.sections(request.matter_id, ref.document_id)
                    if s.section_id == ref.section_id
                ),
                None,
            )
            if current_section is None or validate_evidence_reference(
                ref, version, current_section, text
            ):
                raise MatterError("Source provenance changed.")
        return EvidenceSearchResult(
            matter_id=request.matter_id,
            query=request.query,
            approved_versions=request.approved_versions,
            index_identity=identity,
            hits=tuple(hits),
            diagnostics=EvidenceDiagnostics(
                passage_count=len(refs),
                candidate_count=len(ranked),
                returned_count=len(hits),
            ),
        )

"""Private search operates on live trusted source snapshots only."""

from collections.abc import Iterator, Sequence
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from lextrace.investigations.evidence_search import (
    ApprovedVersion,
    EvidenceFilters,
    EvidenceSearchRequest,
    EvidenceSearchService,
    PrivateIndexSettings,
)
from lextrace.investigations.provenance import validate_evidence_reference
from lextrace.investigations.repository import InvestigationRepository
from lextrace.matter.contracts import MatterError
from lextrace.matter.store import MatterStore
from lextrace.retrieval.models import Vector


class Encoder:
    def encode(self, texts: Sequence[str]) -> Vector:
        return np.asarray(
            [[1 + t.count("apple"), 1 + t.count("pear")] for t in texts],
            dtype=np.float32,
        )


class Scorer:
    def score(self, pairs: Sequence[tuple[str, str]]) -> list[float]:
        return [float(text.count("pear")) for _, text in pairs]


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[tuple[MatterStore, InvestigationRepository]]:
    db = tmp_path / "matter.sqlite3"
    store = MatterStore(db, tmp_path / "private")
    repo = InvestigationRepository(db)
    yield store, repo
    repo.close()
    store.close()


def source(
    store: MatterStore,
    repo: InvestigationRepository,
    text: str = "😀 apple café.\n\npear pear.",
) -> EvidenceSearchRequest:
    matter = store.create_matter("Private")
    doc = store.add_document(matter.matter_id, "private.txt", text.encode())
    version = repo.active_document_version(matter.matter_id, doc.document_id)
    assert version is not None
    return EvidenceSearchRequest(
        matter_id=matter.matter_id,
        query="apple",
        mode="bm25",
        approved_versions=(
            ApprovedVersion(document_id=doc.document_id, version_id=version.version_id),
        ),
    )


def test_isolation_offsets_filters_and_diagnostics(
    runtime: tuple[MatterStore, InvestigationRepository],
) -> None:
    store, repo = runtime
    first = source(store, repo)
    second = source(store, repo)
    service = EvidenceSearchService(store, repo)
    result = service.search(first)
    assert len(result.hits) == 1
    ref = result.hits[0].reference
    version = repo.active_document_version(first.matter_id, ref.document_id)
    assert version is not None
    section = next(
        s
        for s in store.sections(first.matter_id, ref.document_id)
        if s.section_id == ref.section_id
    )
    text = store.document_text(first.matter_id, ref.document_id)
    assert text[ref.start : ref.end] == ref.exact_quote
    assert not validate_evidence_reference(ref, version, section, text)
    assert "😀" in ref.exact_quote
    assert "apple" not in result.diagnostics.model_dump_json()
    assert service.search(second).index_identity != result.index_identity
    with pytest.raises(MatterError):
        service.search(
            first.model_copy(update={"approved_versions": second.approved_versions})
        )
    assert not service.search(
        first.model_copy(update={"filters": EvidenceFilters(document_types=("pdf",))})
    ).hits
    assert not service.search(first.model_copy(update={"query": "absent"})).hits
    assert not service.search(first.model_copy(update={"query": "   "})).hits
    assert not service.search(first.model_copy(update={"approved_versions": ()})).hits


@pytest.mark.parametrize("mode", ["bm25", "dense", "hybrid", "reranked"])
def test_rankings_and_budgets(
    runtime: tuple[MatterStore, InvestigationRepository], mode: str
) -> None:
    store, repo = runtime
    request = source(store, repo)
    service = EvidenceSearchService(store, repo, encoder=Encoder(), reranker=Scorer())
    request = EvidenceSearchRequest.model_validate(
        {
            **request.model_dump(),
            "mode": mode,
            "candidate_budget": 2,
            "result_budget": 1,
        }
    )
    result = service.search(request)
    assert len(result.hits) == 1
    assert result.diagnostics.candidate_count <= 2
    assert result == service.search(request)
    if mode == "reranked":
        assert "pear" in result.hits[0].reference.exact_quote
    elif mode == "bm25":
        assert "apple" in result.hits[0].reference.exact_quote


def test_identity_overlap_and_deletion(
    runtime: tuple[MatterStore, InvestigationRepository],
) -> None:
    store, repo = runtime
    request = source(store, repo, "apple " * 20)
    settings = PrivateIndexSettings(chunk_size=24, overlap=12)
    service = EvidenceSearchService(store, repo, settings=settings)
    result = service.search(request)
    spans = [h.reference for h in result.hits]
    assert len(spans) > 1
    assert all(
        a.end <= b.start or b.end <= a.start
        for i, a in enumerate(spans)
        for b in spans[i + 1 :]
    )
    fixed = request.model_copy(
        update={"expected_index_identity": result.index_identity}
    )
    assert service.search(fixed) == result
    with pytest.raises(MatterError):
        EvidenceSearchService(store, repo).search(fixed)
    approved = request.approved_versions[0]
    assert store.delete_document(request.matter_id, approved.document_id)
    assert not (store.private_root / request.matter_id / approved.document_id).exists()
    with pytest.raises(MatterError):
        service.search(request)


def test_retirement_and_invalid_provenance(
    runtime: tuple[MatterStore, InvestigationRepository],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, repo = runtime
    request = source(store, repo)
    approved = request.approved_versions[0]
    version = repo.active_document_version(request.matter_id, approved.document_id)
    assert version is not None
    service = EvidenceSearchService(store, repo)
    with monkeypatch.context() as patch:
        sections = store.sections(request.matter_id, approved.document_id)
        patch.setattr(
            store,
            "sections",
            lambda *_: [sections[0].model_copy(update={"text": "fabricated"})],
        )
        with pytest.raises(MatterError):
            service.search(request)
    with monkeypatch.context() as patch:
        patch.setattr(store, "sections", lambda *_: [])
        with pytest.raises(MatterError):
            service.search(request)
    repo.create_document_version(
        version.model_copy(update={"version_id": "replacement"})
    )
    with pytest.raises(MatterError):
        service.search(request)


def test_strict_budgets_and_safe_model_failure(
    runtime: tuple[MatterStore, InvestigationRepository],
) -> None:
    store, repo = runtime
    request = source(store, repo)
    with pytest.raises(ValidationError):
        EvidenceSearchRequest.model_validate(
            {**request.model_dump(), "candidate_budget": True}
        )
    with pytest.raises(ValidationError):
        EvidenceSearchRequest.model_validate(
            {**request.model_dump(), "result_budget": 101}
        )
    with pytest.raises(MatterError, match="^Private evidence search failed.$"):
        EvidenceSearchService(store, repo).search(
            request.model_copy(update={"mode": "dense"})
        )


def test_ties_no_network_and_mid_search_deletion(
    runtime: tuple[MatterStore, InvestigationRepository],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Network forbidden")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    store, repo = runtime
    request = source(store, repo, "apple one.\n\napple two.")
    service = EvidenceSearchService(store, repo, encoder=Encoder(), reranker=Scorer())
    result = service.search(request)
    assert [h.reference.start for h in result.hits] == sorted(
        h.reference.start for h in result.hits
    )
    hybrid = request.model_copy(update={"mode": "reranked"})
    assert service.search(hybrid).hits

    class DeletingScorer:
        def score(self, pairs: Sequence[tuple[str, str]]) -> list[float]:
            store.delete_document(
                request.matter_id, request.approved_versions[0].document_id
            )
            return [1.0] * len(pairs)

    with pytest.raises(MatterError):
        EvidenceSearchService(
            store, repo, encoder=Encoder(), reranker=DeletingScorer()
        ).search(hybrid)


def test_hash_change_and_passage_budget_fail_closed(
    runtime: tuple[MatterStore, InvestigationRepository],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, repo = runtime
    request = source(store, repo, "apple " * 20)
    with pytest.raises(MatterError):
        EvidenceSearchService(
            store,
            repo,
            settings=PrivateIndexSettings(chunk_size=12, overlap=0, max_passages=1),
        ).search(request)
    monkeypatch.setattr(store, "document_text", lambda *_: "fabricated apple")
    with pytest.raises(MatterError):
        EvidenceSearchService(store, repo).search(request)

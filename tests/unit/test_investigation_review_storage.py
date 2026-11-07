"""Review restart, isolation, CAS and targeted invalidation regressions."""

import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from hashlib import sha256
from pathlib import Path

import pytest

from lextrace.investigations.contracts import EntityMention, InvestigationEvent
from lextrace.investigations.repository import InvestigationRepository
from lextrace.investigations.review import (
    DateCorrection,
    ProjectionDependency,
    RecordDependency,
    target_of,
)
from lextrace.matter.contracts import (
    DocumentSection,
    MatterDocument,
    MatterError,
    SourceSpan,
)
from lextrace.matter.store import MatterStore
from tests.unit.test_investigation_chronology import event
from tests.unit.test_investigation_repository import legacy as legacy_fixture
from tests.unit.test_investigation_repository import manifest
from tests.unit.test_investigation_review import command

legacy = legacy_fixture
TEXT = "account text"
TEXT_HASH = sha256(TEXT.encode()).hexdigest()


@pytest.fixture
def source_store(legacy: tuple[Path, list[MatterDocument]]) -> Iterator[MatterStore]:
    database, documents = legacy
    store = MatterStore(database, database.parent / "private")
    InvestigationRepository(database).close()
    for document in documents:
        directory = store.private_root / document.matter_id / document.document_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "extracted.txt").write_text(TEXT, encoding="utf-8")
        section = DocumentSection(
            section_id="section",
            document_id=document.document_id,
            order=0,
            kind="paragraph",
            text=TEXT,
            span=SourceSpan(
                document_id=document.document_id,
                section_id="section",
                start=0,
                end=len(TEXT),
            ),
        )
        with sqlite3.connect(database) as db:
            db.execute(
                "INSERT INTO document_sections VALUES (?,?,?)",
                (section.section_id, document.document_id, section.model_dump_json()),
            )
    yield store
    store.close()


def seed(
    repository: InvestigationRepository, document: MatterDocument, identity: str = "e"
) -> InvestigationEvent:
    version = manifest(document, "version").model_copy(
        update={
            "parser": "synthetic",
            "parser_version": "1",
            "extracted_text_hash": TEXT_HASH,
        }
    )
    repository.create_document_version(version)
    candidate = event(
        identity,
        date(2020, 1, 1),
        matter_id=document.matter_id,
        document_id=document.document_id,
    )
    ref = candidate.evidence_references[0].model_copy(
        update={"extracted_text_hash": TEXT_HASH}
    )
    candidate = candidate.model_copy(
        update={
            "evidence_references": (ref,),
            "temporal": candidate.temporal.model_copy(
                update={"evidence_references": (ref,)}
            ),
        }
    )
    repository.register_review_candidate(candidate)
    return candidate


def projection(
    repository: InvestigationRepository,
    candidate: InvestigationEvent | EntityMention,
    name: str,
) -> ProjectionDependency:
    target = target_of(candidate)
    current = repository.current_review(target)
    assert current is not None
    dependency = ProjectionDependency(
        matter_id=target.matter_id,
        projection_id=name,
        inputs=(RecordDependency(target=target, revision=current.revision),),
    )
    repository.save_projection_dependency(dependency)
    return dependency


def test_restart_audit_and_idempotent_replay(
    legacy: tuple[Path, list[MatterDocument]],
    source_store: MatterStore,
) -> None:
    database, documents = legacy
    repository = InvestigationRepository(database, source_store=source_store)
    candidate = seed(repository, documents[0])
    decision = command(candidate)
    accepted = repository.review(decision)
    repository.close()
    repository = InvestigationRepository(database, source_store=source_store)
    assert repository.review(decision) == accepted
    assert repository.current_review(target_of(candidate)) == accepted
    assert repository.register_review_candidate(candidate) == accepted
    deferred = repository.review(
        command(candidate, decision_id="defer", expected_revision=2, decision="DEFER")
    )
    assert deferred.revision == 3
    # Old replay returns its exact result without altering the current revision.
    assert repository.review(decision) == accepted
    assert [
        r.review_state for r in repository.review_history(target_of(candidate))
    ] == ["PROPOSED", "ACCEPTED", "DEFERRED"]
    with pytest.raises(MatterError, match="identity conflicts"):
        repository.review(decision.model_copy(update={"reviewer_id": "other"}))
    repository.close()


def test_concurrent_reviewers(
    legacy: tuple[Path, list[MatterDocument]], source_store: MatterStore
) -> None:
    database, documents = legacy
    repository = InvestigationRepository(database, source_store=source_store)
    candidate = seed(repository, documents[0])
    repository.close()

    def review(number: int) -> str:
        repo = InvestigationRepository(database, source_store=source_store)
        try:
            repo.review(
                command(candidate, reviewer_id=str(number), decision_id=str(number))
            )
            return "saved"
        except MatterError:
            return "conflict"
        finally:
            repo.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(review, range(2))) == ["conflict", "saved"]
    repository = InvestigationRepository(database, source_store=source_store)
    assert len(repository.review_history(target_of(candidate))) == 2
    repository.close()


def test_date_identity_and_unrelated_dependencies(
    legacy: tuple[Path, list[MatterDocument]],
    source_store: MatterStore,
) -> None:
    database, documents = legacy
    repository = InvestigationRepository(database, source_store=source_store)
    candidate = seed(repository, documents[0])
    unrelated = seed(repository, documents[0], "unrelated")
    repository.review(command(candidate))
    projection(repository, candidate, "chronology")
    projection(repository, unrelated, "other")
    assert repository.projection_is_current(
        candidate.matter_id, "chronology", require_accepted=True
    )
    correction = DateCorrection(
        kind="exact",
        earliest=date(2020, 2, 1),
        latest=date(2020, 2, 1),
        precision="day",
    )
    edited = repository.review(
        command(
            candidate,
            decision_id="date",
            expected_revision=2,
            decision="EDIT",
            date_correction=correction,
        )
    )
    assert edited.original == candidate
    assert not repository.projection_is_current(candidate.matter_id, "chronology")
    assert repository.projection_is_current(candidate.matter_id, "other")
    mention = EntityMention(
        **{
            key: value
            for key, value in candidate.model_dump().items()
            if key in EntityMention.model_fields
        },
        mention_id="alias",
        entity_type="person",
        mention_text="account",
    )
    repository.register_review_candidate(mention)
    projection(repository, mention, "identity-dependent")
    resolution = repository.review(
        command(
            mention, decision_id="resolve", decision="EDIT", resolved_entity_id="person"
        )
    )
    assert resolution.original == mention
    assert not repository.projection_is_current(
        candidate.matter_id, "identity-dependent"
    )
    assert repository.projection_is_current(candidate.matter_id, "other")
    repository.close()


def test_source_replacement_deletion_and_isolation(
    legacy: tuple[Path, list[MatterDocument]],
    source_store: MatterStore,
) -> None:
    database, documents = legacy
    repository = InvestigationRepository(database, source_store=source_store)
    candidate = seed(repository, documents[0])
    other = seed(repository, documents[1])
    repository.review(command(candidate))
    projection(repository, candidate, "current")
    projection(repository, other, "other")
    wrong = target_of(candidate).model_copy(update={"matter_id": other.matter_id})
    assert repository.current_review(wrong) is None
    assert repository.review_history(wrong) == ()
    with pytest.raises(MatterError):
        repository.review(command(candidate).model_copy(update={"target": wrong}))
    assert not repository.projection_is_current(other.matter_id, "current")
    repository.create_document_version(manifest(documents[0], "new"))
    assert repository.current_review(target_of(candidate)) is None
    with pytest.raises(MatterError, match="stale"):
        repository.register_review_candidate(candidate)
    assert not repository.projection_is_current(
        candidate.matter_id, "current", require_accepted=True
    )
    assert (
        repository.review_history(target_of(candidate))[-1].review_state == "ACCEPTED"
    )
    with pytest.raises(MatterError, match="stale"):
        repository.review(command(candidate, decision_id="late", expected_revision=2))
    assert repository.projection_is_current(other.matter_id, "other")
    # Delete through the legacy table; audit persists and current refs disappear.
    with sqlite3.connect(database) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute(
            "DELETE FROM matter_documents WHERE id=?", (documents[1].document_id,)
        )
    assert repository.current_review(target_of(other)) is None
    assert not repository.projection_is_current(other.matter_id, "other")
    assert len(repository.review_history(target_of(other))) == 1
    repository.close()
    repository = InvestigationRepository(database, source_store=source_store)
    assert not repository.projection_is_current(other.matter_id, "other")
    repository.close()


def test_invalid_registration_and_projection(
    legacy: tuple[Path, list[MatterDocument]],
    source_store: MatterStore,
) -> None:
    database, documents = legacy
    repository = InvestigationRepository(database, source_store=source_store)
    candidate = seed(repository, documents[0])
    with pytest.raises(MatterError, match="identity conflicts"):
        repository.register_review_candidate(
            candidate.model_copy(update={"description": "changed"})
        )
    ref = candidate.evidence_references[0].model_copy(
        update={"parser_version": "wrong"}
    )
    invalid = candidate.model_copy(
        update={"event_id": "bad", "evidence_references": (ref,)}
    )
    with pytest.raises(MatterError, match="version conflicts"):
        repository.register_review_candidate(invalid)
    dep = projection(repository, candidate, "projection")
    with pytest.raises(MatterError, match="stale"):
        repository.save_projection_dependency(
            dep.model_copy(
                update={
                    "projection_id": "missing",
                    "inputs": (
                        RecordDependency(target=target_of(candidate), revision=99),
                    ),
                }
            )
        )
    assert not repository.projection_is_current(
        candidate.matter_id, "projection", require_accepted=True
    )
    repository.close()


def test_corrections_and_chronology_survive_restart(
    legacy: tuple[Path, list[MatterDocument]],
    source_store: MatterStore,
) -> None:
    database, documents = legacy
    repository = InvestigationRepository(database, source_store=source_store)
    candidate = seed(repository, documents[0])
    correction = DateCorrection(
        kind="exact",
        earliest=date(2020, 2, 1),
        latest=date(2020, 2, 1),
        precision="day",
    )
    repository.review(command(candidate, decision="EDIT", date_correction=correction))
    repository.review(command(candidate, decision_id="accept", expected_revision=2))
    projection(repository, candidate, "chronology")
    repository.close()
    repository = InvestigationRepository(database, source_store=source_store)
    chronology = repository.load_reviewed_chronology(
        candidate.matter_id, "chronology", require_accepted=True
    )
    assert chronology is not None
    assert chronology.dated_groups[0].earliest == date(2020, 2, 1)
    assert chronology.dated_groups[0].events[0].original == candidate
    repository.review(
        command(
            candidate, decision_id="re-review", expected_revision=3, decision="DEFER"
        )
    )
    assert (
        repository.load_reviewed_chronology(candidate.matter_id, "chronology") is None
    )
    repository.close()


def test_deleted_source_identity_cannot_resurrect_acceptance(
    legacy: tuple[Path, list[MatterDocument]],
    source_store: MatterStore,
) -> None:
    database, documents = legacy
    repository = InvestigationRepository(database, source_store=source_store)
    candidate = seed(repository, documents[0])
    repository.review(command(candidate))
    with sqlite3.connect(database) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("DELETE FROM matter_documents WHERE id=?", (candidate.document_id,))
        document = documents[0]
        db.execute(
            "INSERT INTO matter_documents VALUES (?,?,?)",
            (document.document_id, document.matter_id, document.model_dump_json()),
        )
    with pytest.raises(MatterError, match="Retired"):
        seed(repository, documents[0])
    assert repository.current_review(target_of(candidate)) is None
    assert (
        repository.review_history(target_of(candidate))[-1].review_state == "ACCEPTED"
    )
    repository.close()


@pytest.mark.parametrize(
    ("kind", "nested"),
    [
        ("event", False),
        ("event", True),
        ("assertion", False),
        ("assertion", True),
        ("mention", False),
    ],
)
@pytest.mark.parametrize("defect", ["quote", "section", "ownership", "stale"])
def test_registration_verifies_stored_evidence(
    legacy: tuple[Path, list[MatterDocument]],
    source_store: MatterStore,
    kind: str,
    nested: bool,
    defect: str,
) -> None:
    from lextrace.investigations.contracts import AttributedAssertion

    database, documents = legacy
    repository = InvestigationRepository(database, source_store=source_store)
    original = seed(repository, documents[0])
    candidate: InvestigationEvent | AttributedAssertion | EntityMention
    candidate = original.model_copy(update={"event_id": "forged"})
    if kind == "assertion":
        candidate = AttributedAssertion(
            **{
                key: value
                for key, value in original.model_dump().items()
                if key in AttributedAssertion.model_fields and key != "stated_time"
            },
            assertion_id="forged",
            normalized_proposition="An account",
            attribution_status="unknown",
            modality="asserted",
            stated_time=original.temporal,
        )
    elif kind == "mention":
        candidate = EntityMention(
            **{
                key: value
                for key, value in original.model_dump().items()
                if key in EntityMention.model_fields
            },
            mention_id="forged",
            entity_type="person",
            mention_text="account",
        )
    ref = original.evidence_references[0]
    updates = {
        "quote": {"exact_quote": "fiction"},
        "section": {"section_id": "invented"},
        "ownership": {"matter_id": documents[1].matter_id},
        "stale": {"version_id": "old"},
    }[defect]
    forged = ref.model_copy(update=updates)
    if nested:
        temporal = original.temporal.model_copy(
            update={"evidence_references": (forged,)}
        )
        field = "temporal" if kind == "event" else "stated_time"
        candidate = candidate.model_copy(update={field: temporal})
    else:
        candidate = candidate.model_copy(update={"evidence_references": (forged,)})
        if kind == "mention" and defect == "quote":
            candidate = candidate.model_copy(update={"mention_text": "fiction"})
    with pytest.raises((MatterError, ValueError)):
        repository.register_review_candidate(candidate)
    assert repository.review_history(target_of(candidate)) == ()
    # Rejection leaves the transaction usable and prior revisions intact.
    assert repository.register_review_candidate(original).revision == 1
    repository.close()


def test_registration_requires_trusted_text(
    legacy: tuple[Path, list[MatterDocument]],
    source_store: MatterStore,
) -> None:
    database, documents = legacy
    repository = InvestigationRepository(database, source_store=source_store)
    candidate = seed(repository, documents[0])
    repository.close()
    repository = InvestigationRepository(database)
    with pytest.raises(MatterError, match="Trusted source text store is required"):
        repository.register_review_candidate(candidate)
    repository.close()


def test_registration_rejects_changed_stored_text(
    legacy: tuple[Path, list[MatterDocument]],
    source_store: MatterStore,
) -> None:
    database, documents = legacy
    repository = InvestigationRepository(database, source_store=source_store)
    candidate = seed(repository, documents[0])
    path = (
        source_store.private_root
        / candidate.matter_id
        / candidate.document_id
        / "extracted.txt"
    )
    path.write_text("fiction text", encoding="utf-8")
    with pytest.raises(MatterError, match="EXTRACTED_HASH_MISMATCH"):
        repository.register_review_candidate(candidate)
    repository.close()


@pytest.mark.parametrize("defect", ["identity", "ownership", "text"])
def test_registration_rejects_inconsistent_stored_section(
    legacy: tuple[Path, list[MatterDocument]],
    source_store: MatterStore,
    defect: str,
) -> None:
    database, documents = legacy
    repository = InvestigationRepository(database, source_store=source_store)
    candidate = seed(repository, documents[0])
    with sqlite3.connect(database) as db:
        row = db.execute(
            "SELECT payload FROM document_sections WHERE document_id=? AND id=?",
            (candidate.document_id, "section"),
        ).fetchone()
        section = DocumentSection.model_validate_json(row[0])
        updates = {
            "identity": {"section_id": "wrong"},
            "ownership": {"document_id": documents[1].document_id},
            "text": {"text": "fiction text"},
        }[defect]
        section = section.model_copy(update=updates)
        db.execute(
            "UPDATE document_sections SET payload=? WHERE document_id=? AND id=?",
            (section.model_dump_json(), candidate.document_id, "section"),
        )
    with pytest.raises(MatterError, match="invalid provenance"):
        repository.register_review_candidate(candidate)
    assert len(repository.review_history(target_of(candidate))) == 1
    repository.close()

"""Exact source structure, not model actor labels, determines account attribution."""

import json
from pathlib import Path
from typing import Any

import pytest

from lextrace.investigations.contracts import AttributedAssertion
from lextrace.investigations.extraction_verification import verify_extraction
from lextrace.investigations.factual_extraction import extract_factual_candidates
from lextrace.investigations.repository import InvestigationRepository
from lextrace.matter.store import MatterStore
from tests.unit.test_investigation_factual_extraction import FakeLLM

CASES = json.loads(
    (
        Path(__file__).parents[1] / "fixtures/investigations/speaker_accounts.json"
    ).read_text()
)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_source_structure_wins_over_actor_proposal(
    tmp_path: Path, case: dict[str, Any]
) -> None:
    with_store = MatterStore(tmp_path / "db", tmp_path / "private")
    repo = InvestigationRepository(tmp_path / "db", source_store=with_store)
    try:
        matter = with_store.create_matter("Fictional attribution fixture")
        doc = with_store.add_document(
            matter.matter_id, "fiction.txt", case["text"].encode()
        )
        version = repo.active_document_version(matter.matter_id, doc.document_id)
        assert version is not None
        sections = tuple(with_store.sections(matter.matter_id, doc.document_id))
        responses: list[object] = []
        for section in sections:
            proposals = []
            for quote, speaker in zip(case["quotes"], case["proposed"], strict=True):
                if quote in section.text:
                    item = dict(
                        kind="assertion",
                        quote=quote,
                        speaker_quote=speaker,
                        subject_quote=None,
                        predicate_quote=None,
                        object_quote=None,
                    )
                    if item not in proposals:
                        proposals.append(item)
            responses.append({"candidates": proposals})
        extracted = extract_factual_candidates(
            version,
            case["text"],
            sections,
            FakeLLM(responses),
            run_id="test",
            verbatim_anchors=True,
        )
        checked = verify_extraction(
            extracted, version, sections, case["text"], run_id="test"
        )
        assertions = [
            c for c in checked.valid_candidates if isinstance(c, AttributedAssertion)
        ]
        assert len(assertions) == len(case["expected"])
        assert [c.attributed_to for c in assertions] == case["expected"]
        for record in assertions:
            if record.attributed_to:
                ref = record.attribution_reference
                assert ref is not None
                assert case["text"][ref.start : ref.end] == ref.exact_quote
                assert record.attributed_to in ref.exact_quote
    finally:
        repo.close()
        with_store.close()


def test_empty_model_output_does_not_lose_explicit_account(tmp_path: Path) -> None:
    store = MatterStore(tmp_path / "db", tmp_path / "private")
    repo = InvestigationRepository(tmp_path / "db", source_store=store)
    try:
        matter = store.create_matter("Fictional")
        text = "Witness statement: Zoë D’Angelo\n\nI observed Niko Tran carry a parcel."
        doc = store.add_document(matter.matter_id, "fiction.txt", text.encode())
        version = repo.active_document_version(matter.matter_id, doc.document_id)
        assert version is not None
        sections = tuple(store.sections(matter.matter_id, doc.document_id))
        result = extract_factual_candidates(
            version,
            text,
            sections,
            FakeLLM([{"candidates": []}] * len(sections)),
            run_id="test",
            verbatim_anchors=True,
        )
        checked = verify_extraction(result, version, sections, text, run_id="test")
        accounts = [
            c for c in checked.valid_candidates if isinstance(c, AttributedAssertion)
        ]
        assert len(accounts) == 1
        assert accounts[0].attributed_to == "Zoë D’Angelo"
        assert (
            accounts[0].evidence_references[0].exact_quote
            == "I observed Niko Tran carry a parcel."
        )
    finally:
        repo.close()
        store.close()


@pytest.mark.parametrize("damage", ["actor", "matter", "version", "offset"])
def test_independent_attribution_proof_rejects_tampering(damage: str) -> None:
    from dataclasses import replace

    from tests.unit.test_investigation_factual_extraction import source

    text = "Witness statement: Maya Chen\nI saw Jordan Lee move the crate."
    version, sections = source(text)
    result = extract_factual_candidates(
        version,
        text,
        sections,
        FakeLLM([{"candidates": []}]),
        run_id="test",
        verbatim_anchors=True,
    )
    account = next(c for c in result.candidates if isinstance(c, AttributedAssertion))
    proof = account.attribution_reference
    assert proof is not None
    if damage == "actor":
        start = text.index("Jordan Lee")
        proof = proof.model_copy(
            update={
                "start": start,
                "end": start + len("Jordan Lee"),
                "exact_quote": "Jordan Lee",
            }
        )
        account = account.model_copy(
            update={"attributed_to": "Jordan Lee", "attribution_reference": proof}
        )
    else:
        changes: dict[str, object] = (
            {"matter_id": "foreign"}
            if damage == "matter"
            else {"version_id": "stale"}
            if damage == "version"
            else {"start": proof.start + 1}
        )
        account = account.model_copy(
            update={"attribution_reference": proof.model_copy(update=changes)}
        )
    checked = verify_extraction(
        replace(result, candidates=(account,)), version, sections, text, run_id="test"
    )
    assert not checked.valid_candidates
    assert (
        "ATTRIBUTION_STRUCTURE_MISMATCH"
        in checked.quarantined_candidates[0].issue_codes
    )


def test_repeated_exact_quotes_are_not_fuzzily_resolved() -> None:
    from tests.unit.test_investigation_factual_extraction import source

    text = (
        "Speaker: Casey Park\nI arrived at noon.\n\n"
        "Speaker: Casey Park\nI arrived at noon."
    )
    version, sections = source(text)
    llm = FakeLLM(
        [
            {
                "candidates": [
                    dict(
                        kind="assertion",
                        quote="I arrived at noon.",
                        speaker_quote=None,
                        subject_quote=None,
                        predicate_quote=None,
                        object_quote=None,
                    )
                ]
            }
        ]
    )
    result = extract_factual_candidates(
        version, text, sections, llm, run_id="test", verbatim_anchors=True
    )
    assert result.quarantined[0].reason == "AMBIGUOUS_QUOTE"
    checked = verify_extraction(result, version, sections, text, run_id="test")
    accounts = [
        c for c in checked.valid_candidates if isinstance(c, AttributedAssertion)
    ]
    assert len(accounts) == 2
    assert len({c.evidence_references[0].start for c in accounts}) == 2
    assert len({c.assertion_id for c in accounts}) == 2
    assert all(c.generator == "investigation-source-accounts@1.0.0" for c in accounts)
    assert "CANDIDATES_QUARANTINED" in result.coverage.warnings


@pytest.mark.parametrize("windows", [0, 100])
def test_raw_accounts_obey_processed_window_bounds(windows: int) -> None:
    from tests.unit.test_investigation_factual_extraction import source

    text = "Speaker: Priya Shah\nI saw Omar move a parcel and I recorded the time."
    version, sections = source(text)
    result = extract_factual_candidates(
        version,
        text,
        sections,
        FakeLLM([{"candidates": []}] * 100),
        run_id="test",
        verbatim_anchors=True,
        max_characters=20,
        max_windows=windows,
    )
    accounts = [c for c in result.candidates if isinstance(c, AttributedAssertion)]
    assert bool(accounts) == bool(windows)
    for account in accounts:
        ref = account.evidence_references[0]
        assert len(ref.exact_quote) <= 20
        assert text[ref.start : ref.end] == ref.exact_quote
        assert any(
            w.start <= ref.start < ref.end <= w.end
            for w in result.coverage.processed_windows
        )


def test_legacy_assertion_payload_remains_readable_without_new_field() -> None:
    from tests.unit.test_investigation_factual_extraction import source

    text = "Speaker: Priya Shah\nI arrived at noon."
    version, sections = source(text)
    result = extract_factual_candidates(
        version,
        text,
        sections,
        FakeLLM([{"candidates": []}]),
        run_id="test",
        verbatim_anchors=True,
    )
    account = next(c for c in result.candidates if isinstance(c, AttributedAssertion))
    old = account.model_dump(mode="json", exclude={"attribution_reference"})
    old["generator"] = "investigation-factual-extraction@2.0.0"
    restored = AttributedAssertion.model_validate_json(json.dumps(old))
    assert restored.attribution_reference is None
    assert restored.attributed_to == "Priya Shah"

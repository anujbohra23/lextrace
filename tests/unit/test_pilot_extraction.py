"""Pilot regression: real TXT section geometry, never offset repair."""

from pathlib import Path

from lextrace.investigations.extraction_verification import verify_extraction
from lextrace.investigations.factual_extraction import extract_factual_candidates
from lextrace.investigations.repository import InvestigationRepository
from lextrace.matter.store import MatterStore
from tests.unit.test_investigation_factual_extraction import FakeLLM, candidate, source


def test_txt_whitespace_separators_are_inspected_not_missing(tmp_path: Path) -> None:
    store = MatterStore(tmp_path / "db", tmp_path / "private")
    repo = InvestigationRepository(tmp_path / "db", source_store=store)
    try:
        matter = store.create_matter("Synthetic only")
        text = "  Alex statement.\n\nJordan statement.\n"
        doc = store.add_document(matter.matter_id, "fiction.txt", text.encode())
        version = repo.active_document_version(matter.matter_id, doc.document_id)
        assert version is not None
        sections = tuple(store.sections(matter.matter_id, doc.document_id))
        llm = FakeLLM([{"candidates": []}] * len(sections))
        result = extract_factual_candidates(version, text, sections, llm, run_id="test")
        verified = verify_extraction(result, version, sections, text, run_id="test")
        assert result.coverage.status == verified.coverage.status == "COMPLETE"
        assert not result.coverage.skipped_spans
        assert not verified.coverage.uncovered_spans
        assert len(llm.contexts) == len(sections)  # no model call for separators
        assert store.document_text(matter.matter_id, doc.document_id) == text
    finally:
        repo.close()
        store.close()


def test_substantive_unsectioned_text_remains_partial() -> None:
    version, _ = source("omitted account")
    result = extract_factual_candidates(
        version, "omitted account", (), FakeLLM([]), run_id="test"
    )
    assert result.coverage.status == "PARTIAL"
    assert result.coverage.skipped_spans[0].reason == "NO_TRUSTED_SECTION"


def test_offsets_stay_strict_and_diagnostics_are_specific() -> None:
    version, sections = source("Alex")
    result = extract_factual_candidates(
        version,
        "Alex",
        sections,
        FakeLLM([{"candidates": [candidate("Alex", 1)]}]),
        run_id="test",
    )
    assert not result.candidates
    assert result.quarantined[0].reason == "INVALID_RELATIVE_OFFSETS"


def test_unique_verbatim_quote_can_use_explicit_server_anchor() -> None:
    quote = "Alex Morgan states: I arrived."
    text = "😀 Intro. " + quote
    version, sections = source(text)
    item = candidate(quote, kind="assertion", speaker_quote="Alex Morgan") | {
        "start": None,
        "end": None,
    }
    result = extract_factual_candidates(
        version, text, sections, FakeLLM([{"candidates": [item]}]), run_id="test"
    )
    assert len(result.candidates) == 1
    record = result.candidates[0]
    assert record.evidence_references[0].exact_quote == quote
    assert record.evidence_references[0].start == text.index(quote)
    assert record.attributed_to == "Alex Morgan"  # type: ignore[union-attr]


def test_server_anchor_rejects_ambiguous_quotes_and_unsourced_speaker() -> None:
    for text, item in [
        ("Alex / Alex", candidate("Alex") | {"start": None, "end": None}),
        (
            "Alex states: I arrived.",
            candidate("I arrived.", kind="assertion", speaker_quote="Jordan")
            | {"start": None, "end": None},
        ),
    ]:
        version, sections = source(text)
        result = extract_factual_candidates(
            version, text, sections, FakeLLM([{"candidates": [item]}]), run_id="test"
        )
        assert not result.candidates
        assert len(result.quarantined) == 1


def test_production_quote_schema_has_no_offset_or_metadata_repair() -> None:
    version, sections = source("Alex states: I arrived.")
    for item in [
        {"kind": "event", "quote": "Alex states: I arrived.", "start": 1, "end": 2},
        {"kind": "event", "quote": "Alex states: I arrived.", "entity_type": "person"},
    ]:
        result = extract_factual_candidates(
            version,
            "Alex states: I arrived.",
            sections,
            FakeLLM([{"candidates": [item]}]),
            run_id="test",
            verbatim_anchors=True,
        )
        assert not result.candidates
        assert result.coverage.status == "PARTIAL"
        assert result.coverage.incomplete_reasons == (
            "MODEL_FAILURE_OR_MALFORMED_OUTPUT",
        )


def test_safe_diagnostic_counters_do_not_log_source(caplog: object) -> None:
    import logging

    import pytest

    assert isinstance(caplog, pytest.LogCaptureFixture)
    text = "PRIVATE synthetic quotation"
    version, sections = source(text)
    with caplog.at_level(logging.INFO):
        result = extract_factual_candidates(
            version,
            text,
            sections,
            FakeLLM([{"candidates": [candidate(text, kind="event")]}]),
            run_id="test",
        )
    assert result.candidates
    record = next(
        r for r in caplog.records if r.message == "factual_extraction_summary"
    )
    counts = vars(record)["extraction_counts"]
    assert counts["proposed_by_kind"] == {"event": 1}
    assert counts["accepted"] == 1
    assert text not in caplog.text
    assert text not in repr(counts)


def test_source_predicate_anchor_reaches_existing_m6_descriptor_field() -> None:
    text = "Alex states: I arrived at nine."
    version, sections = source(text)
    item = dict(
        kind="assertion",
        quote=text,
        speaker_quote="Alex",
        predicate_quote="arrived",
        subject_quote="I",
        object_quote="at nine",
    )
    result = extract_factual_candidates(
        version,
        text,
        sections,
        FakeLLM([{"candidates": [item]}]),
        run_id="test",
        verbatim_anchors=True,
    )
    assert len(result.candidates) == 1
    assert result.candidates[0].predicate == "arrived"  # type: ignore[union-attr]
    item["predicate_quote"] = "manufactured"
    invalid = extract_factual_candidates(
        version,
        text,
        sections,
        FakeLLM([{"candidates": [item]}]),
        run_id="test",
        verbatim_anchors=True,
    )
    assert not invalid.candidates
    assert invalid.quarantined[0].reason == "UNSOURCED_PREDICATE"

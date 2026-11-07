"""Offline service checks against adversarial and bounded structured responses."""

from hashlib import sha256
from typing import TypeVar

import pytest
from pydantic import BaseModel

from lextrace.investigations.contracts import (
    AttributedAssertion,
    DocumentVersion,
    EntityMention,
    InvestigationEvent,
    TemporalReference,
)
from lextrace.investigations.factual_extraction import extract_factual_candidates
from lextrace.investigations.provenance import validate_evidence_reference
from lextrace.matter.contracts import DocumentSection, SourceSpan
from lextrace.research.contracts import Usage
from lextrace.research.prompts import PromptDefinition

Output = TypeVar("Output", bound=BaseModel)


class FakeLLM:
    provider = "fake"
    model = "deterministic"
    retries = 0

    def __init__(self, responses: list[object]) -> None:
        self.responses = iter(responses)
        self.usage = Usage()
        self.contexts: list[dict[str, object]] = []

    def generate(
        self,
        prompt: PromptDefinition,
        schema: type[Output],
        context: dict[str, object],
    ) -> Output:
        self.contexts.append(context)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return schema.model_validate(response)


def source(text: str) -> tuple[DocumentVersion, tuple[DocumentSection, ...]]:
    version = DocumentVersion(
        matter_id="trusted-matter",
        document_id="trusted-document",
        version_id="trusted-version",
        original_content_hash="0" * 64,
        extracted_text_hash=sha256(text.encode()).hexdigest(),
        parser="synthetic",
        parser_version="1",
        extraction_settings_hash="1" * 64,
        document_type="txt",
        text_length=len(text),
        coverage="COMPLETE",
    )
    sections = (
        (
            DocumentSection(
                section_id="section",
                document_id=version.document_id,
                order=0,
                kind="paragraph",
                text=text,
                span=SourceSpan(
                    document_id=version.document_id,
                    section_id="section",
                    start=0,
                    end=len(text),
                ),
            ),
        )
        if text
        else ()
    )
    return version, sections


def candidate(
    quote: str, start: int = 0, kind: str = "entity", **changes: object
) -> dict[str, object]:
    return (
        dict(
            kind=kind,
            start=start,
            end=start + len(quote),
            quote=quote,
            entity_type="person" if kind == "entity" else None,
            temporal_kind="relative" if kind == "temporal" else None,
        )
        | changes
    )


def test_exact_repeated_unicode_and_source_isolation() -> None:
    text = "😀 Alice / Alice"
    version, sections = source(text)
    llm = FakeLLM([{"candidates": [candidate("Alice", 2), candidate("Alice", 10)]}])
    result = extract_factual_candidates(version, text, sections, llm, run_id="run")
    assert result.coverage.status == "COMPLETE"
    assert not result.quarantined
    assert (
        len({c.mention_id for c in result.candidates if isinstance(c, EntityMention)})
        == 2
    )
    assert [c.evidence_references[0].start for c in result.candidates] == [2, 10]
    for record in result.candidates:
        assert record.version_id == version.version_id
        assert record.matter_id == version.matter_id
        assert record.document_id == version.document_id
        assert (
            validate_evidence_reference(
                record.evidence_references[0], version, sections[0], text
            )
            == ()
        )
    assert llm.contexts == [{"text": text}]


@pytest.mark.parametrize(
    "item",
    [
        candidate("Ghost"),
        candidate("Alice", -1),
        candidate("Alice", 100),
        candidate("Alice", end=0),
        candidate("Alice", temporal_kind="relative"),
    ],
)
def test_invalid_candidates_quarantined(item: dict[str, object]) -> None:
    version, sections = source("Alice")
    result = extract_factual_candidates(
        version, "Alice", sections, FakeLLM([{"candidates": [item]}]), run_id="run"
    )
    assert not result.candidates
    assert len(result.quarantined) == 1


def test_accounts_and_dates_never_become_findings() -> None:
    text = "Bob alleged a visit next day in May 2020."
    version, sections = source(text)
    items = [
        candidate(text, kind="assertion"),
        candidate(text, kind="event"),
        candidate("next day", text.index("next day"), "temporal"),
        candidate(
            "May 2020", text.index("May 2020"), "temporal", temporal_kind="approximate"
        ),
    ]
    result = extract_factual_candidates(
        version, text, sections, FakeLLM([{"candidates": items}]), run_id="run"
    )
    assertion, event, relative, partial = result.candidates
    assert isinstance(assertion, AttributedAssertion)
    assert assertion.attributed_to is None
    assert assertion.attribution_status == "unresolved"
    assert assertion.stated_time is None
    assert isinstance(event, InvestigationEvent)
    assert event.temporal.kind == "unknown"
    assert not event.participants and not event.fact_ids
    assert isinstance(relative, TemporalReference)
    assert relative.raw_expression == "next day"
    assert isinstance(partial, TemporalReference)
    assert partial.raw_expression == "May 2020"
    for record in (assertion, event, event.temporal, relative, partial):
        assert record.review_state == "PROPOSED"
        assert record.review_decision_id is None
        assert record.verification_state == "UNVERIFIED"
        assert record.interpretation_basis == "SOURCE_ASSERTION"
    for temporal in (event.temporal, relative, partial):
        assert temporal.earliest is None and temporal.latest is None
        assert temporal.precision == "unknown"


@pytest.mark.parametrize(
    "response",
    [
        RuntimeError("sensitive provider error"),
        {"candidates": "invalid"},
        {"candidates": [candidate("Alice", version_id="hostile")]},
        {"candidates": [candidate("Alice", review_state="ACCEPTED")]},
        {"candidates": [candidate("Alice", attributed_to="Wrong speaker")]},
    ],
)
def test_model_failures_and_forbidden_metadata(response: object) -> None:
    version, sections = source("Alice")
    result = extract_factual_candidates(
        version, "Alice", sections, FakeLLM([response]), run_id="run"
    )
    assert not result.candidates
    assert result.coverage.status == "PARTIAL"
    assert (
        result.coverage.skipped_spans[0].reason == "MODEL_FAILURE_OR_MALFORMED_OUTPUT"
    )
    assert "sensitive" not in repr(result)


def test_budget_and_section_boundaries() -> None:
    text = "AliceBob"
    version, sections = source(text)
    first = sections[0].model_copy(
        update={"text": "Alice", "span": sections[0].span.model_copy(update={"end": 5})}
    )
    second = DocumentSection(
        section_id="second",
        document_id=version.document_id,
        order=1,
        kind="paragraph",
        text="Bob",
        span=SourceSpan(
            document_id=version.document_id, section_id="second", start=5, end=8
        ),
    )
    llm = FakeLLM([{"candidates": []}])
    result = extract_factual_candidates(
        version,
        text,
        (second, first),
        llm,
        run_id="run",
        max_characters=6,
        max_windows=1,
    )
    assert llm.contexts == [{"text": "Alice"}]
    assert result.coverage.status == "PARTIAL"
    assert result.coverage.skipped_spans[0].start == 5
    assert result.coverage.skipped_spans[0].reason == "WINDOW_BUDGET_EXHAUSTED"


def test_split_windows_use_relative_offsets() -> None:
    version, sections = source("AliceAlice")
    llm = FakeLLM([{"candidates": [candidate("Alice")]}] * 2)
    result = extract_factual_candidates(
        version, "AliceAlice", sections, llm, run_id="run", max_characters=5
    )
    assert [c.evidence_references[0].start for c in result.candidates] == [0, 5]
    assert result.coverage.status == "COMPLETE"
    assert all(len(str(c["text"])) <= 5 for c in llm.contexts)


@pytest.mark.parametrize("text", ["", "scanned"])
def test_ocr_excluded(text: str) -> None:
    version, sections = source(text)
    version = version.model_copy(update={"coverage": "OCR_REQUIRED"})
    llm = FakeLLM([])
    result = extract_factual_candidates(version, text, sections, llm, run_id="run")
    assert not result.candidates and not llm.contexts
    assert result.coverage.status == "OCR_REQUIRED"
    assert result.coverage.incomplete_reasons == ("OCR_REQUIRED",)


def test_empty_partial_and_unsectioned_sources() -> None:
    for text, source_status in [
        ("", "COMPLETE"),
        ("Alice", "PARTIAL"),
        ("Alice", "COMPLETE"),
    ]:
        version, _ = source(text)
        result = extract_factual_candidates(
            version.model_copy(update={"coverage": source_status}),
            text,
            (),
            FakeLLM([]),
            run_id="run",
        )
        assert result.coverage.status == "PARTIAL"
        assert result.coverage.incomplete_reasons


@pytest.mark.parametrize("change", ["text", "document", "section_text", "overlap"])
def test_invalid_trusted_input_fails_before_generation(change: str) -> None:
    version, sections = source("Alice")
    if change == "text":
        version = version.model_copy(update={"extracted_text_hash": "f" * 64})
    elif change == "document":
        sections = (sections[0].model_copy(update={"document_id": "other"}),)
    elif change == "section_text":
        sections = (sections[0].model_copy(update={"text": "Ghost"}),)
    else:
        sections = sections * 2
    llm = FakeLLM([])
    with pytest.raises(ValueError):
        extract_factual_candidates(version, "Alice", sections, llm, run_id="run")
    assert not llm.contexts

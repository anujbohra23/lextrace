"""Offline exact-source contracts and adversarial provenance checks."""

from hashlib import sha256

import pytest
from pydantic import ValidationError

from lextrace.investigations.contracts import DocumentVersion, EvidenceReference
from lextrace.investigations.provenance import validate_evidence_reference
from lextrace.matter.contracts import DocumentSection, SourceSpan

TEXT = "📜 lead\nSame 😀 e\u0301. / Same 😀 e\u0301.\nend"
QUOTE = "Same 😀 e\u0301."


@pytest.fixture
def source() -> tuple[EvidenceReference, DocumentVersion, DocumentSection]:
    identity = dict(
        matter_id="matter",
        document_id="document",
        version_id="version",
        original_content_hash=sha256(b"original").hexdigest(),
        extracted_text_hash=sha256(TEXT.encode()).hexdigest(),
        parser="synthetic",
        parser_version="1",
        extraction_settings_hash=sha256(b"{}").hexdigest(),
    )
    version = DocumentVersion.model_validate(
        dict(
            **identity,
            document_type="txt",
            text_length=len(TEXT),
            coverage="COMPLETE",
        )
    )
    start, end = TEXT.index(QUOTE), TEXT.rindex(QUOTE) + len(QUOTE)
    section = DocumentSection(
        section_id="section",
        document_id="document",
        order=0,
        kind="paragraph",
        text=TEXT[start:end],
        span=SourceSpan(
            section_id="section",
            document_id="document",
            start=start,
            end=end,
        ),
    )
    ref = EvidenceReference.model_validate(
        dict(
            **identity,
            section_id="section",
            start=start,
            end=start + len(QUOTE),
            exact_quote=QUOTE,
        )
    )
    return ref, version, section


def test_unicode_and_distinct_repeated_offsets(
    source: tuple[EvidenceReference, DocumentVersion, DocumentSection],
) -> None:
    ref, version, section = source
    for start in (TEXT.index(QUOTE), TEXT.rindex(QUOTE)):
        candidate = ref.model_copy(update={"start": start, "end": start + len(QUOTE)})
        assert validate_evidence_reference(candidate, version, section, TEXT) == ()
    shifted = ref.model_copy(update={"start": ref.start + 1, "end": ref.end + 1})
    assert "QUOTE_MISMATCH" in validate_evidence_reference(
        shifted, version, section, TEXT
    )
    assert EvidenceReference.model_validate_json(ref.model_dump_json()) == ref
    assert DocumentVersion.model_validate_json(version.model_dump_json()) == version


@pytest.mark.parametrize(
    ("field", "value", "issue"),
    [
        ("matter_id", "other", "MATTER_MISMATCH"),
        ("document_id", "other", "DOCUMENT_MISMATCH"),
        ("version_id", "other", "VERSION_MISMATCH"),
        ("original_content_hash", "0" * 64, "ORIGINAL_HASH_MISMATCH"),
        ("extracted_text_hash", "0" * 64, "EXTRACTED_HASH_MISMATCH"),
        ("parser", "other", "EXTRACTION_IDENTITY_MISMATCH"),
        ("parser_version", "2", "EXTRACTION_IDENTITY_MISMATCH"),
        ("extraction_settings_hash", "0" * 64, "EXTRACTION_IDENTITY_MISMATCH"),
        ("section_id", "other", "SECTION_MISMATCH"),
        ("exact_quote", "Same 😀 é.", "QUOTE_MISMATCH"),
        ("start", 0, "SECTION_CONTAINMENT_MISMATCH"),
        ("end", len(TEXT) + 1, "SPAN_BOUNDS_INVALID"),
        ("start", -1, "MALFORMED_REFERENCE"),
        ("start", TEXT.index(QUOTE) + len(QUOTE), "MALFORMED_REFERENCE"),
        ("end", 0, "MALFORMED_REFERENCE"),
        ("exact_quote", "", "MALFORMED_REFERENCE"),
    ],
)
def test_reject_reference_changes(
    source: tuple[EvidenceReference, DocumentVersion, DocumentSection],
    field: str,
    value: object,
    issue: str,
) -> None:
    ref, version, section = source
    candidate = ref.model_copy(update={field: value})
    assert issue in validate_evidence_reference(candidate, version, section, TEXT)


def test_source_and_section_integrity(
    source: tuple[EvidenceReference, DocumentVersion, DocumentSection],
) -> None:
    ref, version, section = source
    assert "EXTRACTED_HASH_MISMATCH" in validate_evidence_reference(
        ref,
        version,
        section,
        TEXT.replace("lead", "LEAD"),
    )
    assert "TEXT_LENGTH_MISMATCH" in validate_evidence_reference(
        ref, version, section, ""
    )
    for update, issue in [
        ({"document_id": "other"}, "DOCUMENT_MISMATCH"),
        ({"section_id": "other"}, "SECTION_MISMATCH"),
        ({"text": "different"}, "SECTION_TEXT_MISMATCH"),
    ]:
        changed = section.model_copy(update=update)
        assert issue in validate_evidence_reference(ref, version, changed, TEXT)
    span_cases: list[tuple[dict[str, object], str]] = [
        ({"document_id": "other"}, "DOCUMENT_MISMATCH"),
        ({"section_id": "other"}, "SECTION_MISMATCH"),
        ({"end": len(TEXT) + 1}, "SECTION_BOUNDS_INVALID"),
    ]
    for span_update, issue in span_cases:
        changed = section.model_copy(
            update={"span": section.span.model_copy(update=span_update)}
        )
        assert issue in validate_evidence_reference(ref, version, changed, TEXT)
    assert validate_evidence_reference(ref, version, section, "\ud800") == (
        "INVALID_UNICODE",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("start", True),
        ("start", "1"),
        ("start", -1),
        ("end", 0),
        ("exact_quote", ""),
        ("matter_id", " "),
        ("original_content_hash", "bad"),
        ("unknown", "extra"),
        ("page_start", 1),
    ],
)
def test_strict_schema(
    source: tuple[EvidenceReference, DocumentVersion, DocumentSection],
    field: str,
    value: object,
) -> None:
    ref, _, _ = source
    with pytest.raises(ValidationError):
        EvidenceReference.model_validate(ref.model_dump() | {field: value})


def test_immutability_and_manifest_schema(
    source: tuple[EvidenceReference, DocumentVersion, DocumentSection],
) -> None:
    ref, version, _ = source
    for record in (ref, version):
        with pytest.raises(ValidationError):
            record.matter_id = "changed"
    for update in (
        {"unknown": 1},
        {"text_length": -1},
        {"page_starts": (0,)},
        {"extraction_settings_hash": "bad"},
        {"parser_version": ""},
    ):
        with pytest.raises(ValidationError):
            DocumentVersion.model_validate(version.model_dump() | update)


def test_pages(
    source: tuple[EvidenceReference, DocumentVersion, DocumentSection],
) -> None:
    ref, version, section = source
    paged = ref.model_copy(update={"page_start": 1, "page_end": 1})
    pdf = version.model_copy(update={"document_type": "pdf", "page_starts": (0,)})
    assert validate_evidence_reference(paged, pdf, section, TEXT) == ()
    assert "PAGE_MISMATCH" in validate_evidence_reference(paged, version, section, TEXT)
    docx = version.model_copy(update={"document_type": "docx"})
    assert validate_evidence_reference(ref, docx, section, TEXT) == ()
    assert "PAGE_MISMATCH" in validate_evidence_reference(paged, docx, section, TEXT)


@pytest.mark.parametrize(
    ("update", "issue"),
    [
        ({"text_length": len(TEXT) + 1}, "TEXT_LENGTH_MISMATCH"),
        ({"extracted_text_hash": "0" * 64}, "EXTRACTED_HASH_MISMATCH"),
        ({"parser_version": "2"}, "EXTRACTION_IDENTITY_MISMATCH"),
        ({"text_length": -1}, "MALFORMED_VERSION"),
    ],
)
def test_changed_manifest(
    source: tuple[EvidenceReference, DocumentVersion, DocumentSection],
    update: dict[str, object],
    issue: str,
) -> None:
    ref, version, section = source
    assert issue in validate_evidence_reference(
        ref, version.model_copy(update=update), section, TEXT
    )

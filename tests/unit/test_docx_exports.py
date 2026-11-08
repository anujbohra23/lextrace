# ruff: noqa: F811 -- shared synthetic fixtures
"""Editable, source-grounded Word package content and bounded layout helpers."""

import io
import re
from zipfile import ZipFile

import pytest
from docx import Document

from lextrace.deposition.contracts import PlanView
from lextrace.exports import document, render
from lextrace.exports.document import (
    ExportError,
    WordReport,
    attachment_name,
    word_text,
)
from lextrace.exports.render import ReportInput
from tests.deposition_fixtures import deposition as deposition  # noqa: F401
from tests.exports_fixtures import approved_plan as approved_plan  # noqa: F401
from tests.exports_fixtures import report_input as report_input  # noqa: F401
from tests.unit.test_investigation_durable import durable as durable  # noqa: F401
from tests.unit.test_investigation_workflow import setup as memory_setup  # noqa: F401


def paragraphs(content: bytes) -> list[str]:
    return [p.text for p in Document(io.BytesIO(content)).paragraphs]


def test_investigation_is_valid_editable_word_with_full_references(
    report_input: ReportInput,
) -> None:
    download = render.investigation(report_input)
    package = ZipFile(io.BytesIO(download.content))
    assert package.testzip() is None
    assert {"[Content_Types].xml", "word/document.xml", "word/styles.xml"}.issubset(
        package.namelist()
    )
    doc = Document(io.BytesIO(download.content))
    assert len(doc.inline_shapes) == 0
    assert doc.paragraphs[0].style is not None
    assert doc.paragraphs[0].style.name == "Title"
    text = "\n".join(paragraphs(download.content))
    for title in (
        "Executive summary",
        "Reviewed chronology",
        "Reviewed evidence accounts",
        "Findings and potential inconsistencies",
        "Source reference appendix",
    ):
        assert title in text
    assert "No reviewed chronology events were recorded" in text
    assert report_input.context.snapshot.snapshot_id in text
    assert "Investigation approval time: Not recorded" in text
    for record in report_input.context.revisions:
        for ref in record.original.evidence_references:
            for value in (
                ref.document_id,
                ref.version_id,
                ref.section_id,
                ref.exact_quote,
                f"[{ref.start}, {ref.end})",
                record.original.run_id,
            ):
                assert value in text
    assert b"PAGE" in package.read("word/footer1.xml")
    assert "potential inconsistency" in text.lower()
    assert "not a proven contradiction" in text


def test_deposition_uses_approved_wording_in_exact_order(
    report_input: ReportInput, approved_plan: PlanView
) -> None:
    approved = approved_plan.current
    questions = tuple(
        q.model_copy(
            update={
                "order": len(approved.questions) - i - 1,
                "wording": f"Approved wording {i + 1}?",
            }
        )
        for i, q in enumerate(approved.questions)
    )
    view = approved_plan.model_copy(
        update={"current": approved.model_copy(update={"questions": questions})}
    )
    data = ReportInput(
        report_input.matter, report_input.context, view, approved.revision + 1
    )
    text = "\n".join(paragraphs(render.deposition(data).content))
    assert "Deposition Preparation" in text
    assert "Plan approval time:" in text
    assert "Historical approved revision" in text
    assert "later drafts or decisions are not included" in text
    assert "Account scoped witness ID" in text
    assert "Equal or similar names have not been merged" in text
    ordered = sorted(questions, key=lambda q: q.order)
    assert [text.index(q.wording) for q in ordered] == sorted(
        text.index(q.wording) for q in ordered
    )
    for q in questions:
        assert q.rationale in text
    assert "not established testimony" in text


def test_fixed_input_gives_fixed_editable_document_content(
    report_input: ReportInput,
) -> None:
    first, second = (
        render.investigation(report_input),
        render.investigation(report_input),
    )
    assert paragraphs(first.content) == paragraphs(second.content)
    assert ZipFile(io.BytesIO(first.content)).read("word/document.xml") == ZipFile(
        io.BytesIO(second.content)
    ).read("word/document.xml")


def test_long_unicode_control_characters_and_safe_names() -> None:
    sample = "Référence 東京 😀\twords\nnext\r\x00\x0b\u202e"
    safe = word_text(sample)
    assert safe == "Référence 東京 😀\twords\nnext\\u000d\\u0000\\u000b\\u202e"
    report = WordReport("Investigation Report")
    report.add(sample)
    report.add("Long quotation " + "語" * 5000, "Quote")
    content = report.finish()
    assert safe in paragraphs(content)
    name = attachment_name(
        '../../"Référence\r\n東京/' + "a" * 200, "deposition", "f" * 64, 2
    )
    assert re.fullmatch(r"[A-Za-z0-9.-]+\.docx", name)
    assert len(name) < 110 and ".." not in name


@pytest.mark.parametrize(
    "limit,value",
    [("MAX_TEXT_CHARACTERS", 10), ("MAX_PARAGRAPHS", 1), ("MAX_DOCX_BYTES", 10)],
)
def test_report_limits_fail_without_truncated_documents(
    monkeypatch: pytest.MonkeyPatch, limit: str, value: int
) -> None:
    monkeypatch.setattr(document, limit, value)
    with pytest.raises(ExportError, match="EXPORT_TOO_LARGE"):
        report = WordReport("Investigation Report")
        report.add("A bounded source quote")
        report.finish()


def test_source_registry_is_bounded(
    report_input: ReportInput, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(document, "MAX_REFERENCES", 0)
    with pytest.raises(ExportError, match="EXPORT_TOO_LARGE"):
        render.investigation(report_input)


def test_reviewed_chronology_keeps_date_ties_and_unknown_dates(
    report_input: ReportInput,
) -> None:
    from dataclasses import replace
    from datetime import date

    from lextrace.investigations.review import apply_review, initial_review
    from lextrace.investigations.reviewed_chronology import build_reviewed_chronology
    from tests.unit.test_investigation_chronology import event
    from tests.unit.test_investigation_review import command

    ref = report_input.context.revisions[0].original.evidence_references[0]
    originals = (
        event("known-a", date(2020, 1, 1)),
        event("known-b", date(2020, 1, 1)),
        event("unknown", kind="unknown", precision="unknown"),
    )
    events = []
    for original in originals:
        base = {
            "matter_id": ref.matter_id,
            "document_id": ref.document_id,
            "version_id": ref.version_id,
            "evidence_references": (ref,),
        }
        original = original.model_copy(
            update={**base, "temporal": original.temporal.model_copy(update=base)}
        )
        events.append(apply_review(initial_review(original), command(original)))
    chronology = build_reviewed_chronology(
        tuple(events), matter_id=ref.matter_id, require_accepted=True
    )
    artifacts = replace(report_input.context.artifacts, reviewed_chronology=chronology)
    context = replace(
        report_input.context,
        artifacts=artifacts,
        revisions=(*report_input.context.revisions, *events),
    )
    text = "\n".join(
        paragraphs(
            render.investigation(ReportInput(report_input.matter, context)).content
        )
    )
    assert "Date range 2020-01-01 to 2020-01-01" in text
    assert "Order within a group is unresolved" in text
    assert "Events with unresolved dates" in text
    assert "known-a" in text and "known-b" in text and "unknown" in text


def test_oversized_text_is_rejected_before_sanitization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = WordReport("Investigation Report")
    monkeypatch.setattr(document, "MAX_TEXT_CHARACTERS", 100)

    def forbidden(value: object) -> str:
        raise AssertionError("Oversized text reached the character renderer")

    monkeypatch.setattr(document, "word_text", forbidden)
    with pytest.raises(ExportError, match="EXPORT_TOO_LARGE"):
        report.add("x" * 101)

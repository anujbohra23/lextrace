"""Small python-docx layout/citation layer; no remote templates or report storage."""

import io
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from docx import Document
from docx.document import Document as WordDocument
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from docx.styles.style import ParagraphStyle
from docx.text.paragraph import Paragraph

from lextrace.investigations.contracts import EvidenceReference
from lextrace.investigations.inconsistency_investigation import evidence_id

DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAX_TEXT_CHARACTERS = 2_000_000
MAX_PARAGRAPHS = 10_000
MAX_REFERENCES = 2_000
MAX_DOCX_BYTES = 8 * 1024 * 1024


class ExportError(Exception):
    def __init__(self, code: str, status: int = 409) -> None:
        super().__init__(code)
        self.code, self.status = code, status


def word_text(value: object) -> str:
    """Keep source whitespace; visibly escape unsafe XML/directional controls.

    CR is escaped because Word otherwise converts it into LF. No quote is silently
    corrected or truncated. Exact immutable originals remain in source storage.
    """
    output = []
    for char in str(value):
        number = ord(char)
        unsafe = (number < 32 and char not in "\t\n") or (
            0x7F <= number <= 0x9F
            or 0xD800 <= number <= 0xDFFF
            or number & 0xFFFF in (0xFFFE, 0xFFFF)
            or 0x202A <= number <= 0x202E
            or 0x2066 <= number <= 0x2069
        )
        output.append(f"\\u{number:04x}" if unsafe else char)
    return "".join(output)


def attachment_name(
    matter_name: str, kind: str, identity: str, revision: int | None = None
) -> str:
    slug = unicodedata.normalize("NFKD", matter_name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^A-Za-z0-9]+", "-", slug).strip("-")[:48] or "matter"
    token = re.sub(r"[^a-zA-Z0-9]", "", identity)[:12] or "report"
    suffix = f"-r{revision}" if revision is not None else ""
    # kind is a renderer-owned constant, not a client filename or path.
    return f"{slug}-{kind}-{token}{suffix}.docx"


@dataclass(frozen=True)
class Download:
    content: bytes
    filename: str


class Sources:
    def __init__(self) -> None:
        self.references: dict[str, EvidenceReference] = {}
        self.runs: dict[str, set[str]] = {}
        self.labels: dict[str, str] = {}

    def add(
        self, reference: EvidenceReference, extraction_run: str | None = None
    ) -> None:
        if len(reference.exact_quote) > MAX_TEXT_CHARACTERS:
            raise ExportError("EXPORT_TOO_LARGE")
        identity = evidence_id(reference)
        self.references[identity] = reference
        if extraction_run:
            self.runs.setdefault(identity, set()).add(extraction_run)
        if len(self.references) > MAX_REFERENCES:
            raise ExportError("EXPORT_TOO_LARGE")

    def freeze(self) -> None:
        self.labels = {
            key: f"S{i:03d}" for i, key in enumerate(sorted(self.references), 1)
        }

    def cite(self, refs: tuple[EvidenceReference, ...]) -> str:
        try:
            return "Sources " + ", ".join(self.labels[evidence_id(ref)] for ref in refs)
        except KeyError:
            raise ExportError("EXPORT_SOURCE_CONFLICT") from None

    def cite_ids(self, identities: tuple[str, ...]) -> str:
        if not identities:
            return "No source references recorded for this assessment"
        try:
            return "Sources " + ", ".join(self.labels[key] for key in identities)
        except KeyError:
            raise ExportError("EXPORT_SOURCE_CONFLICT") from None


class WordReport:
    def __init__(self, title: str) -> None:
        self.document: WordDocument = Document()
        self.characters = 0
        self.paragraphs = 0
        section = self.document.sections[0]
        section.page_width, section.page_height = Inches(8.5), Inches(11)
        section.top_margin = section.bottom_margin = Inches(0.85)
        section.left_margin = section.right_margin = Inches(1)
        section.header_distance = section.footer_distance = Inches(0.35)
        for name, size in (
            ("Normal", 11),
            ("Title", 24),
            ("Subtitle", 14),
            ("Header", 9),
            ("Footer", 9),
            ("Heading 1", 15),
            ("Heading 2", 12),
            ("Heading 3", 11),
            ("Quote", 10),
        ):
            style = cast(ParagraphStyle, self.document.styles[name])
            style.font.name = "Arial"
            style.font.size = Pt(size)
            style.font.color.rgb = RGBColor(0, 0, 0)
            style.font.italic = False
            fonts = style.element.get_or_add_rPr().get_or_add_rFonts()
            fonts.set(qn("w:eastAsia"), "Arial Unicode MS")
            for attribute in ("asciiTheme", "eastAsiaTheme", "hAnsiTheme", "cstheme"):
                fonts.attrib.pop(qn(f"w:{attribute}"), None)
            # The library's bundled template can carry a decorative title rule.
            # No high-level python-docx paragraph-border property exists.
            for border in style.element.xpath("./w:pPr/w:pBdr"):
                border.getparent().remove(border)
            style.paragraph_format.keep_with_next = (
                name.startswith("Heading") or name == "Title"
            )
            style.paragraph_format.space_after = Pt(7)
            style.paragraph_format.line_spacing = 1.12
            style.paragraph_format.widow_control = True
        quote = cast(ParagraphStyle, self.document.styles["Quote"])
        quote.font.italic = False
        quote.paragraph_format.left_indent = Inches(0.22)
        quote.paragraph_format.right_indent = Inches(0.1)
        quote.paragraph_format.keep_with_next = False
        self.document.core_properties.title = title
        self.document.core_properties.author = "LexTrace"
        self.document.core_properties.last_modified_by = "LexTrace"
        self.document.core_properties.created = datetime.now(UTC)
        self.document.core_properties.modified = datetime.now(UTC)
        section.header.paragraphs[0].text = "LexTrace  |  Reviewed source material"
        footer = section.footer.paragraphs[0]
        footer.text = "Preparation material  |  Page "
        # python-docx has no high-level page-number field API.
        page = OxmlElement("w:fldSimple")
        page.set(
            "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}instr",
            "PAGE",
        )
        footer._p.append(page)
        self.add(title, "Title")

    def add(self, value: object, style: str = "Normal") -> Paragraph:
        raw = str(value)
        if len(raw) + self.characters > MAX_TEXT_CHARACTERS:
            raise ExportError("EXPORT_TOO_LARGE")
        text = word_text(raw)
        self.characters += len(text)
        self.paragraphs += 1
        if self.characters > MAX_TEXT_CHARACTERS or self.paragraphs > MAX_PARAGRAPHS:
            raise ExportError("EXPORT_TOO_LARGE")
        paragraph = self.document.add_paragraph(text, style)
        if text.startswith("Sources "):
            paragraph.paragraph_format.keep_with_next = True
        return paragraph

    def heading(self, value: str, level: int = 1) -> None:
        self.add(value, f"Heading {level}")

    def metadata(self, label: str, value: object) -> Paragraph:
        paragraph = self.add(f"{label}: {value}")
        paragraph.paragraph_format.space_after = Pt(4)
        for run in paragraph.runs:
            run.font.size = Pt(9)
        return paragraph

    def appendix(self, sources: Sources) -> None:
        self.heading("Source reference appendix")
        self.add(
            "Quotations preserve recorded source text. Unsafe XML"
            " controls, carriage returns and directional "
            "overrides are displayed as literal Unicode escapes "
            "such as \\u000d. Sources remain unchanged. Character "
            "spans use half open Python Unicode code point "
            "offsets."
        )
        if not sources.references:
            self.add("No reviewed source references were recorded.")
        for key in sorted(sources.references):
            ref = sources.references[key]
            self.heading(sources.labels[key], 2)
            for label, value in (
                ("Reference identity", key),
                ("Matter", ref.matter_id),
                ("Document", ref.document_id),
                ("Version", ref.version_id),
                ("Section", ref.section_id),
                ("Character span", f"[{ref.start}, {ref.end})"),
                (
                    "Page range",
                    f"{ref.page_start} to {ref.page_end}"
                    if ref.page_start is not None
                    else "Unavailable",
                ),
                (
                    "Extraction runs",
                    ", ".join(sorted(sources.runs.get(key, ())))
                    or "Not attached to an extraction account",
                ),
                ("Original content SHA256", ref.original_content_hash),
                ("Extracted text SHA256", ref.extracted_text_hash),
                ("Parser", ref.parser),
                ("Parser version", ref.parser_version),
                ("Extraction settings SHA256", ref.extraction_settings_hash),
            ):
                self.metadata(label, value)
            self.add(ref.exact_quote, "Quote")

    def finish(self) -> bytes:
        stream = io.BytesIO()
        self.document.save(stream)
        if stream.tell() > MAX_DOCX_BYTES:
            raise ExportError("EXPORT_TOO_LARGE")
        return stream.getvalue()

"""Pure normalization from CourtListener responses into LexTrace models."""

import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from pydantic import HttpUrl, ValidationError

from lextrace.domain.case import Case, Opinion
from lextrace.ingestion.courtlistener import (
    ClusterResponse,
    DocketResponse,
    IngestionError,
    OpinionResponse,
)


class _TextExtractor(HTMLParser):
    # Only structural tags introduce newlines; source whitespace never does.
    paragraphs = {
        "p",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "pre",
        "blockquote",
        "section",
        "article",
        "table",
        "ul",
        "ol",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden: list[str] = []
        self.after_inline = False

    def _boundary(self, tag: str) -> None:
        if tag in self.paragraphs:
            self.parts.append("\n\n")
        elif tag in {"br", "tr", "li"}:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append(" ")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.after_inline = False
        if tag in {"script", "style"}:
            self.hidden.append(tag)
        elif not self.hidden:
            self._boundary(tag)

    def handle_endtag(self, tag: str) -> None:
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
        elif tag != "br":
            self._boundary(tag)
            self.after_inline = tag in {
                "em",
                "i",
                "b",
                "strong",
                "a",
                "span",
                "extracted-citation",
            }

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            # Pretty-printed inline markup can leave indentation before punctuation.
            # Do not rewrite spaces already present inside source text nodes.
            prefix = re.match(r"\s+", data)
            if (
                self.after_inline
                and prefix is not None
                and "\n" in prefix[0]
                and data[len(prefix[0]) :].startswith((".", ",", ";", ":", "!", "?"))
            ):
                while self.parts and not self.parts[-1].strip(" "):
                    self.parts.pop()
                if self.parts:
                    self.parts[-1] = self.parts[-1].rstrip(" ")
                data = data.lstrip()
            self.parts.append(re.sub(r"\s+", " ", data))
            self.after_inline = False


def _html_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    text = re.sub(r" +", " ", "".join(parser.parts))
    text = re.sub(r" *\n *", "\n", text)
    # Adjacent start/end boundaries and empty blocks must not accumulate.
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _source_url(path: str) -> HttpUrl:
    try:
        parsed = urlsplit(path)
    except ValueError:
        raise IngestionError(
            "CourtListener returned an invalid public case path."
        ) from None
    if (
        not path.startswith("/opinion/")
        or path.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or re.search(r"[\\\s%]", path)
        or any(part in {".", ".."} for part in path.split("/"))
    ):
        raise IngestionError("CourtListener returned an invalid public case path.")
    return HttpUrl(urljoin("https://www.courtlistener.com", path))


def normalize_case(
    cluster: ClusterResponse, docket: DocketResponse, opinions: list[OpinionResponse]
) -> Case:
    """Preserve separate opinions and fail rather than emit incomplete text."""
    normalized: list[Opinion] = []
    try:
        for opinion in opinions:
            text = _html_text(opinion.html_with_citations or "")
            field = "html_with_citations"
            if not text:
                text = (opinion.plain_text or "").strip()
                field = "plain_text"
            if not text:
                raise IngestionError("CourtListener opinion text is unavailable.")
            normalized.append(
                Opinion(
                    source_id=str(opinion.id),
                    kind=(opinion.type or "").strip() or "unknown",
                    text=text,
                    text_source_field="html_with_citations"
                    if field == "html_with_citations"
                    else "plain_text",
                )
            )
        return Case(
            source_id=str(cluster.id),
            source_url=_source_url(cluster.absolute_url),
            name=cluster.case_name,
            date_filed=cluster.date_filed,
            court_id=docket.court_id,
            docket_number=(docket.docket_number or "").strip() or None,
            reporter_citations=(
                [
                    " ".join(
                        part
                        for part in (citation.volume, citation.reporter, citation.page)
                        if part
                    )
                    for citation in cluster.citations
                ]
                if cluster.citations is not None
                else None
            ),
            opinions=normalized,
        )
    except ValidationError:
        raise IngestionError(
            "CourtListener data could not be normalized into a case."
        ) from None

"""Conservative, exact text-structure attribution. Labels never merge identities.

Only explicit statement/speaker headings and named speech prefixes are supported.
This recognizes a source's attribution, not the speaker's real identity or truth.
No filename, actor mention, fuzzy quotation or model-supplied identity is trusted.
"""

import re
from dataclasses import dataclass

from lextrace.investigations.contracts import (
    DocumentVersion,
    EvidenceReference,
    SourceIdentity,
)
from lextrace.matter.contracts import DocumentSection

_HEADER = re.compile(
    r"(?:(?:witness\s+)?statement\s+(?:of|by)\s+|"
    r"(?:witness\s+statement|statement|speaker|witness)\s*:\s*)(?P<name>.+)$",
    re.IGNORECASE,
)
_SUFFIX = re.compile(r"(?P<name>.+?)\s+(?:witness\s+)?statement$", re.IGNORECASE)
_PREFIX = re.compile(
    r"(?:^|(?<=[.!?])\s+)(?P<name>[\w’'.-]+(?:[ \t]+[\w’'.-]+){0,5}?)"
    r"\s+(?:states|said|reports|reported|testified|wrote)\s*:",
    re.IGNORECASE,
)
_STOP = {
    "and",
    "or",
    "unknown",
    "anonymous",
    "unidentified",
    "witness",
    "witnesses",
    "speaker",
    "speakers",
    "statement",
    "statements",
    "notes",
    "interview",
    "i",
    "we",
    "he",
    "she",
    "they",
    "you",
}


def _name(value: str) -> bool:
    words = value.split()
    return (
        bool(words)
        and len(words) <= 6
        and all(
            word.casefold() not in _STOP
            and all(c.isalpha() or c in "’'.-" for c in word)
            and any(c.isalpha() for c in word)
            and (word[0].isupper() or word in {"de", "van", "von", "da"})
            for word in words
        )
    )


@dataclass(frozen=True)
class SourceAccount:
    start: int
    end: int
    speaker: str
    attribution: EvidenceReference
    mention: EvidenceReference


def source_accounts(
    version: DocumentVersion,
    sections: tuple[DocumentSection, ...],
    text: str,
) -> tuple[SourceAccount, ...]:
    """Caller first validates text hash and section membership, as M3 already does.

    Unknown headings, missing sections, Q/A and conflicting nested attribution
    terminate the current scope. Repeated labels retain independent source spans.
    No sentence/calendar interpretation or paraphrase is produced.
    """
    identity = {k: getattr(version, k) for k in SourceIdentity.model_fields}
    ordered = sorted(sections, key=lambda s: s.span.start)
    index = 0
    current: tuple[str, EvidenceReference, EvidenceReference] | None = None
    start = 0
    heading_scope = False
    result: list[SourceAccount] = []

    def ref(section: DocumentSection, a: int, b: int) -> EvidenceReference:
        return EvidenceReference(
            **identity,
            section_id=section.section_id,
            start=a,
            end=b,
            exact_quote=text[a:b],
        )

    def flush(end: int) -> None:
        if current and text[start:end].strip():
            result.append(SourceAccount(start, end, *current))

    offset = 0
    for raw in text.splitlines(keepends=True):
        a = offset + len(raw) - len(raw.lstrip())
        b = offset + len(raw.rstrip())
        offset += len(raw)
        if b <= a:
            continue
        while index < len(ordered) and ordered[index].span.end <= a:
            index += 1
        section = ordered[index] if index < len(ordered) else None
        if section is None or not section.span.start <= a < b <= section.span.end:
            flush(a)
            current = None
            continue
        line = text[a:b]
        heading = _HEADER.fullmatch(line) or _SUFFIX.fullmatch(line)
        prefix = list(_PREFIX.finditer(line))
        if heading:
            flush(a)
            heading_scope = True
            name = heading.group("name").strip()
            if _name(name):
                # Name is captured verbatim; trim only grammar whitespace.
                name_start = a + heading.start("name")
                current = (
                    name,
                    ref(section, a, b),
                    ref(section, name_start, name_start + len(name)),
                )
            else:
                current = None
            start = offset
        elif prefix:
            match = prefix[0]
            name = match.group("name")
            if len(prefix) != 1 or not _name(name):
                flush(a)
                current = None
                continue
            anchor = ref(section, a + match.start("name"), a + match.end())
            mention = ref(section, a + match.start("name"), a + match.end("name"))
            if current and not heading_scope:
                flush(a)
                current = (name, anchor, mention)
                start = a + match.start("name")
            elif current and current[0].casefold() == name.casefold():
                # Two explicit cues agree within this source scope. Prefer the
                # local spelling, never merge dossiers or entity identities.
                current = (name, anchor, mention)
            elif current:
                flush(a)
                current = None  # a conflicting speaker inside an account
            else:
                current = (name, anchor, mention)
                start = a + match.start("name")
                heading_scope = False
        elif (
            re.match(r"^(?:Q|A)\s*:", line)
            or re.match(
                r"^(?:witnesses|speakers|statement|interview|notes|log|exhibit)\b",
                line,
                re.I,
            )
            or (line.isupper() and not line.endswith((".", "!", "?")))
            or (
                line == section.text
                and len(line) < 120
                and not line.endswith((".", "!", "?"))
                and not re.match(r"^(?:I|We|My|Our)\b", line)
            )
        ):
            flush(a)
            current = None
        if len(result) > 200:
            return tuple(result)  # caller reports the bounded region overflow
    flush(len(text))
    return tuple(result)


def attribution_for(
    accounts: tuple[SourceAccount, ...], start: int, end: int
) -> SourceAccount | None:
    matches = [a for a in accounts if a.start <= start < end <= a.end]
    return matches[0] if len(matches) == 1 else None

"""Deterministic excerpt proposals, never human legal review or invented text."""

import re

from lextrace.domain.case import Case, Opinion
from lextrace.evaluation.benchmark import (
    BenchmarkError,
    QuerySelection,
    Removal,
    Span,
    digest,
)
from lextrace.evaluation.benchmark_build import _check_leakage, _scrub


def propose_excerpt(case: Case, opinion: Opinion, terms: list[str]) -> QuerySelection:
    """First passing 260-word window at paragraph starts in the first half.

    Remove known names/citations and detectable reporter/short-form references
    using exact source spans. Coherence and aliases still require human review.
    """
    text = opinion.text
    patterns = [
        r"(?<!\w)" + r"\s+".join(re.escape(word) for word in term.split()) + r"(?!\w)"
        for term in terms
        if term.strip()
    ]
    patterns.extend(
        [
            r"\b\d+\s+(?:U\.?\s*S\.?|S\.?\s*Ct\.?|F\.?\s*(?:2d|3d|4th|Supp\.?|App'?x))\s*\d+(?:,\s*\d+(?:[-–]\d+)?)?",
            r"\b(?:id\.|ibid\.|supra\b|infra\b)(?:\s+at\s+\d+(?:[-–]\d+)?)?",
        ]
    )
    starts = [0, *(m.end() for m in re.finditer(r"\n\s*\n", text))]
    for start in starts:
        if start > len(text) // 2:
            break
        words = list(re.finditer(r"\S+", text[start:]))
        if len(words) < 180:
            continue
        end = start + words[min(260, len(words)) - 1].end()
        matches = sorted(
            (start + m.start(), start + m.end())
            for pattern in patterns
            for m in re.finditer(pattern, text[start:end], re.IGNORECASE)
        )
        spans: list[Removal] = []
        for first, last in matches:
            if spans and first <= spans[-1].end:
                spans[-1] = Removal(
                    start=spans[-1].start,
                    end=max(last, spans[-1].end),
                    reason="citation_bearing_clause",
                )
            else:
                spans.append(
                    Removal(start=first, end=last, reason="citation_bearing_clause")
                )
        selection = QuerySelection(
            query_id="q" + case.source_id,
            source_case_id=case.source_id,
            source_opinion_id=opinion.source_id,
            source_text_sha256=digest(text),
            excerpt=Span(start=start, end=end),
            removals=spans,
            review_status="review_required",
            selection_note=(
                "Automated first passing introductory window; coherence, aliases, "
                "and legal context require human review."
            ),
        )
        query = _scrub(selection, text)
        if 150 <= len(query.split()) <= 300:
            try:
                _check_leakage(query, terms)
            except BenchmarkError:
                continue
            return selection
    raise BenchmarkError(
        "No introductory excerpt passed checks; manual selection required."
    )

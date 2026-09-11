"""Paragraph-aware non-overlapping windows with exact source-opinion offsets."""

import math
import re
from collections import Counter

from lextrace.domain.case import Case
from lextrace.evaluation.benchmark import digest
from lextrace.retrieval.bm25 import tokenize
from lextrace.retrieval.contracts import Passage, RetrievalError
from lextrace.retrieval.settings import PassageSettings


def segment(case: Case, settings: PassageSettings) -> list[Passage]:
    result = []
    for opinion in case.opinions:
        for paragraph in re.finditer(
            r"\S(?:.*?\S)?(?=\n\s*\n|\Z)", opinion.text, re.DOTALL
        ):
            words = list(re.finditer(r"\S+", paragraph.group()))
            for first in range(0, len(words), settings.words):
                selected = words[first : first + settings.words]
                start = paragraph.start() + selected[0].start()
                end = paragraph.start() + selected[-1].end()
                text = opinion.text[start:end]
                result.append(
                    Passage(
                        passage_id=digest(
                            f"{case.source_id}:{opinion.source_id}:{start}:{end}:{text}"
                        )[:24],
                        opinion_id=opinion.source_id,
                        start=start,
                        end=end,
                        text=text,
                    )
                )
    if not result:
        raise RetrievalError("Case has no usable passages.")
    return result


def select(
    passages: list[Passage], query: str, idf: dict[str, float], count: int
) -> list[Passage]:
    terms = set(tokenize(query))
    scored = []
    for passage in passages:
        tokens = Counter(tokenize(passage.text))
        score = sum(
            idf.get(term, 1.0) * min(tokens[term], 3) for term in sorted(terms)
        ) / math.sqrt(max(1, sum(tokens.values())))
        scored.append(passage.model_copy(update={"score": score}))
    # Stable original opinion/window order breaks ties.
    return sorted(scored, key=lambda p: -p.score)[:count]

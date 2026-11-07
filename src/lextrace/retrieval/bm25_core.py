"""Shared text/opaque-ID Okapi BM25; positive Robertson IDF."""

import math
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from lextrace.evaluation.benchmark import BenchmarkError


def tokenize(text: str) -> list[str]:
    """Unicode letters/digits, lowercase; no stemming, stop words, or corrections."""
    return re.findall(r"[^\W_]+", text.lower(), flags=re.UNICODE)


@dataclass(frozen=True)
class BM25Config:
    k1: float = 1.2
    b: float = 0.75

    def __post_init__(self) -> None:
        if not math.isfinite(self.k1) or self.k1 <= 0 or not 0 <= self.b <= 1:
            raise BenchmarkError("Invalid BM25 parameters.")


class BM25Core:
    """Rank text records with corpus-wide statistics and pre-ranking ID filters.

    Ties default to lexicographic opaque IDs. Adapters can supply a stable key
    (e.g. numeric case IDs or a document/version/offset ordinal).
    """

    def __init__(
        self,
        documents: list[tuple[str, str]],
        config: BM25Config | None = None,
        *,
        tie_breaker: Callable[[str], int | str] = str,
    ) -> None:
        ids = [identifier for identifier, _ in documents]
        if not ids or len(ids) != len(set(ids)) or any(not i for i in ids):
            raise BenchmarkError(
                "BM25 requires distinct nonempty IDs and a nonempty corpus."
            )
        self._tie_breaker = tie_breaker
        self.config = config or BM25Config()
        self.documents = {
            identifier: Counter(tokenize(text)) for identifier, text in documents
        }
        self.lengths = {
            key: sum(terms.values()) for key, terms in self.documents.items()
        }
        if any(length == 0 for length in self.lengths.values()):
            raise BenchmarkError(
                "BM25 encountered a document without searchable tokens."
            )
        self.average_length = sum(self.lengths.values()) / len(documents)
        df: Counter[str] = Counter()
        for terms in self.documents.values():
            df.update(terms.keys())
        self.idf = {
            term: math.log(1 + (len(documents) - count + 0.5) / (count + 0.5))
            for term, count in df.items()
        }

    def search(
        self,
        query_text: str,
        *,
        document_ids: set[str] | None = None,
        top_k: int | None = None,
    ) -> list[tuple[str, float]]:
        """Return ID/score pairs; filtering does not change corpus statistics."""
        query = Counter(tokenize(query_text))
        scores: dict[str, float] = {}
        for identifier, document in self.documents.items():
            if document_ids is not None and identifier not in document_ids:
                continue
            norm = self.config.k1 * (
                1
                - self.config.b
                + self.config.b * self.lengths[identifier] / self.average_length
            )
            scores[identifier] = sum(
                count
                * self.idf.get(term, 0)
                * document[term]
                * (self.config.k1 + 1)
                / (document[term] + norm)
                for term, count in sorted(query.items())
            )
        ordered = sorted(scores, key=lambda key: (-scores[key], self._tie_breaker(key)))
        if top_k is not None:
            if top_k < 1:
                raise BenchmarkError("top_k must be positive.")
            ordered = ordered[:top_k]
        return [(key, scores[key]) for key in ordered]

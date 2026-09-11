"""Deterministic case-level Okapi BM25 with positive Robertson IDF."""

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field

from lextrace.domain.case import Case
from lextrace.evaluation.benchmark import BenchmarkError, Record, SourceId, Text


def tokenize(text: str) -> list[str]:
    """Unicode letters/digits, lowercase; no stemming, stop words, or corrections."""
    return re.findall(r"[^\W_]+", text.lower(), flags=re.UNICODE)


class RankedResult(Record):
    query_id: Text
    case_id: SourceId
    rank: Annotated[int, Field(strict=True, gt=0)]
    score: Annotated[float, Field(allow_inf_nan=False)]
    method: Literal["bm25"] = "bm25"
    run_id: Text


@dataclass(frozen=True)
class BM25Config:
    k1: float = 1.2
    b: float = 0.75

    def __post_init__(self) -> None:
        if not math.isfinite(self.k1) or self.k1 <= 0 or not 0 <= self.b <= 1:
            raise BenchmarkError("Invalid BM25 parameters.")


class BM25:
    def __init__(self, cases: list[Case], config: BM25Config | None = None) -> None:
        ids = [case.source_id for case in cases]
        if (
            not ids
            or len(ids) != len(set(ids))
            or any(not re.fullmatch(r"[1-9][0-9]*", i) for i in ids)
        ):
            raise BenchmarkError(
                "BM25 requires distinct numeric case IDs and a nonempty corpus."
            )
        self.config = config or BM25Config()
        self.documents = {
            case.source_id: Counter(
                tokenize("\n\n".join(op.text for op in case.opinions))
            )
            for case in cases
        }
        self.lengths = {
            key: sum(terms.values()) for key, terms in self.documents.items()
        }
        if any(length == 0 for length in self.lengths.values()):
            raise BenchmarkError(
                "BM25 encountered a document without searchable tokens."
            )
        self.average_length = sum(self.lengths.values()) / len(cases)
        df: Counter[str] = Counter()
        for terms in self.documents.values():
            df.update(terms.keys())
        self.idf = {
            term: math.log(1 + (len(cases) - count + 0.5) / (count + 0.5))
            for term, count in df.items()
        }

    def rank(
        self,
        query_id: str,
        query_text: str,
        run_id: str,
        *,
        case_ids: set[str] | None = None,
        top_k: int | None = None,
    ) -> list[RankedResult]:
        query = Counter(tokenize(query_text))
        scores: dict[str, float] = {}
        for identifier, document in self.documents.items():
            if case_ids is not None and identifier not in case_ids:
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
        ordered = sorted(scores, key=lambda key: (-scores[key], int(key)))
        if top_k is not None:
            if top_k < 1:
                raise BenchmarkError("top_k must be positive.")
            ordered = ordered[:top_k]
        return [
            RankedResult(
                query_id=query_id,
                case_id=key,
                rank=rank,
                score=scores[key],
                run_id=run_id,
            )
            for rank, key in enumerate(ordered, 1)
        ]

"""Compatibility adapter for public case-level BM25 retrieval."""

import re
from typing import Annotated

from pydantic import Field

from lextrace.domain.case import Case
from lextrace.evaluation.benchmark import BenchmarkError, Record, SourceId, Text
from lextrace.retrieval.bm25_core import BM25Config as BM25Config
from lextrace.retrieval.bm25_core import BM25Core
from lextrace.retrieval.bm25_core import tokenize as tokenize
from lextrace.retrieval.contracts import Mode


class RankedResult(Record):
    query_id: Text
    case_id: SourceId
    rank: Annotated[int, Field(strict=True, gt=0)]
    score: Annotated[float, Field(allow_inf_nan=False)]
    method: Mode = "bm25"
    run_id: Text


class BM25(BM25Core):
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
        super().__init__(
            [
                (case.source_id, "\n\n".join(op.text for op in case.opinions))
                for case in cases
            ],
            config,
            tie_breaker=int,
        )

    def rank(
        self,
        query_id: str,
        query_text: str,
        run_id: str,
        *,
        case_ids: set[str] | None = None,
        top_k: int | None = None,
    ) -> list[RankedResult]:
        rows = self.search(query_text, document_ids=case_ids, top_k=top_k)
        return [
            RankedResult(
                query_id=query_id,
                case_id=key,
                rank=rank,
                score=score,
                run_id=run_id,
            )
            for rank, (key, score) in enumerate(rows, 1)
        ]

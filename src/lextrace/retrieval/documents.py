"""Deterministic read-only retrieval view over canonical Case records."""

import re
from pathlib import Path

from lextrace.corpus import CorpusError, read_cases, serialize_cases
from lextrace.domain.case import Case
from lextrace.evaluation.benchmark import digest
from lextrace.retrieval.contracts import RetrievalError, SearchFilters


class Corpus:
    def __init__(self, cases: list[Case]) -> None:
        ids = [c.source_id for c in cases]
        if (
            not ids
            or len(ids) != len(set(ids))
            or any(re.fullmatch(r"[1-9][0-9]*", i) is None for i in ids)
        ):
            raise RetrievalError(
                "Corpus requires unique positive case IDs and records."
            )
        self.cases = sorted(
            (c.model_copy(deep=True) for c in cases), key=lambda c: int(c.source_id)
        )
        self.by_id = {c.source_id: c for c in self.cases}
        self.ids = tuple(c.source_id for c in self.cases)
        self.hash = digest(serialize_cases(self.cases))

    @classmethod
    def load(cls, path: Path) -> "Corpus":
        try:
            return cls(read_cases(path))
        except CorpusError:
            raise RetrievalError("Could not load a valid normalized corpus.") from None

    def text(self, identifier: str) -> str:
        return "\n\n".join(op.text for op in self.by_id[identifier].opinions)

    def eligible(self, filters: SearchFilters | None) -> set[str]:
        if filters is None:
            return set(self.ids)
        return {
            c.source_id
            for c in self.cases
            if (not filters.courts or c.court_id in filters.courts)
            and (
                not filters.filed_after
                or c.date_filed is not None
                and c.date_filed >= filters.filed_after
            )
            and (
                not filters.filed_before
                or c.date_filed is not None
                and c.date_filed <= filters.filed_before
            )
        }

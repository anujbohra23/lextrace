"""Reciprocal Rank Fusion over unique eligible case rankings."""

from lextrace.retrieval.contracts import RetrievalError


def rrf(
    lexical: list[str], dense: list[str], constant: int = 60
) -> list[tuple[str, float]]:
    if constant < 1:
        raise RetrievalError("RRF constant must be positive.")
    scores: dict[str, float] = {}
    for ranked in (lexical, dense):
        seen: set[str] = set()
        for identifier in ranked:
            if identifier in seen:
                continue
            seen.add(identifier)
            scores[identifier] = scores.get(identifier, 0) + 1 / (constant + len(seen))
    return sorted(scores.items(), key=lambda row: (-row[1], int(row[0])))

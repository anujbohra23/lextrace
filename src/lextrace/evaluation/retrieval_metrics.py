"""Binary citation recovery metrics; unjudged candidates have computational gain 0."""

import math
from statistics import mean

from lextrace.evaluation.benchmark import BenchmarkError


def metrics(ranked_ids: list[str], positives: set[str]) -> dict[str, float]:
    if len(ranked_ids) != len(set(ranked_ids)):
        raise BenchmarkError("Duplicate ranked case IDs.")
    if not positives:
        raise BenchmarkError("Citation recovery requires at least one positive.")
    result = {
        f"recall@{k}": len(set(ranked_ids[:k]) & positives) / len(positives)
        for k in (5, 10, 20)
    }
    result["mrr"] = next(
        (1 / rank for rank, key in enumerate(ranked_ids, 1) if key in positives), 0.0
    )
    dcg = sum(
        1 / math.log2(rank + 1)
        for rank, key in enumerate(ranked_ids[:10], 1)
        if key in positives
    )
    ideal = sum(
        1 / math.log2(rank + 1) for rank in range(1, min(10, len(positives)) + 1)
    )
    result["ndcg@10"] = dcg / ideal
    return result


def macro(rows: list[dict[str, float]]) -> dict[str, float]:
    if not rows:
        raise BenchmarkError("Cannot aggregate an empty query split.")
    return {key: mean(row[key] for row in rows) for key in rows[0]}

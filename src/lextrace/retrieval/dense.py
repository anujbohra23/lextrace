"""Exact cosine search over block-scanned, memory-mapped case vectors."""

import heapq

from lextrace.retrieval.contracts import RetrievalError
from lextrace.retrieval.models import Vector, unit


def dense_search(
    vectors: Vector,
    ids: tuple[str, ...],
    query: Vector,
    eligible: set[str],
    top_k: int,
    block_size: int,
) -> list[tuple[str, float]]:
    query = unit(query)
    if query.shape != (1, vectors.shape[1]):
        raise RetrievalError("Query embedding dimension mismatch.")
    heap: list[tuple[float, int, int]] = []
    for start in range(0, len(ids), block_size):
        scores = vectors[start : start + block_size] @ query[0]
        for offset, score in enumerate(scores):
            position = start + offset
            if ids[position] not in eligible:
                continue
            value = (float(score), -int(ids[position]), position)
            if len(heap) < top_k:
                heapq.heappush(heap, value)
            elif value > heap[0]:
                heapq.heapreplace(heap, value)
    return [(ids[position], score) for score, _, position in sorted(heap, reverse=True)]

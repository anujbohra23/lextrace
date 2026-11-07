"""Hand-computed ranking/metric contracts; no network or external model."""

import json
import math
from datetime import date
from pathlib import Path

import pytest
from pydantic import HttpUrl, ValidationError

from lextrace.domain.case import Case, Opinion
from lextrace.evaluation.benchmark import BenchmarkError
from lextrace.evaluation.retrieval_metrics import macro, metrics
from lextrace.retrieval.bm25 import BM25, BM25Config, RankedResult, tokenize
from lextrace.retrieval.bm25_core import BM25Core


def case(identifier: str, *texts: str) -> Case:
    return Case(
        source_id=identifier,
        source_url=HttpUrl(f"https://www.courtlistener.com/opinion/{identifier}/x/"),
        name="Metadata never indexed",
        docket_number=None,
        date_filed=date(2000, 1, 1),
        court_id="ca2",
        opinions=[
            Opinion(
                source_id=str(100 + int(identifier) + i),
                kind="010combined",
                text=text,
                text_source_field="plain_text",
            )
            for i, text in enumerate(texts)
        ],
    )


def test_tokenizer() -> None:
    assert tokenize("ÉTAT §25 inter-state Rev.Code _ABC") == [
        "état",
        "25",
        "inter",
        "state",
        "rev",
        "code",
        "abc",
    ]


def test_ranking_ties_and_all_opinions() -> None:
    cases = [case("10", "zebra"), case("2", "apple", "zebra zebra")]
    index = BM25(cases)
    assert index.rank("q", "apple zebra", "r")[0].case_id == "2"
    assert [r.case_id for r in index.rank("q", "metadata", "r")] == ["2", "10"]
    assert index.rank("q", "apple", "r") == BM25(list(reversed(cases))).rank(
        "q", "apple", "r"
    )
    assert len({r.case_id for r in index.rank("q", "apple", "r")}) == 2
    assert index.documents["2"]["zebra"] == 2


def test_bm25_hand_computation() -> None:
    result = BM25([case("1", "apple apple"), case("2", "pear pear")]).rank(
        "q", "apple", "r"
    )
    assert result[0].score == pytest.approx(math.log(2) * 2 * 2.2 / 3.2)
    assert result[1].score == 0


@pytest.mark.parametrize(
    "documents", [[], [case("1", "x"), case("1", "y")], [case("1", "!!!")]]
)
def test_invalid_index(documents: list[Case]) -> None:
    with pytest.raises(BenchmarkError):
        BM25(documents)


@pytest.mark.parametrize(
    "k1,b", [(0, 0.75), (float("nan"), 0.75), (1.2, 2), (1.2, float("nan"))]
)
def test_parameters(k1: float, b: float) -> None:
    with pytest.raises(BenchmarkError):
        BM25Config(k1=k1, b=b)


@pytest.mark.parametrize(
    "positives,expected", [({"1"}, 1.0), ({"2"}, 0.5), ({"99"}, 0.0)]
)
def test_mrr(positives: set[str], expected: float) -> None:
    assert metrics(["1", "2", "3"], positives)["mrr"] == expected


def test_multiple_positives() -> None:
    row = metrics([str(i) for i in range(1, 31)], {"1", "6", "21"})
    assert row["recall@5"] == pytest.approx(1 / 3)
    assert row["recall@10"] == pytest.approx(2 / 3)
    assert row["recall@20"] == pytest.approx(2 / 3)
    assert row["ndcg@10"] == pytest.approx(
        (1 + 1 / math.log2(7)) / (1 + 1 / math.log2(3) + 0.5)
    )
    assert macro([row, row]) == row


def test_metric_validation() -> None:
    with pytest.raises(BenchmarkError):
        metrics(["1", "1"], {"1"})
    with pytest.raises(BenchmarkError):
        metrics(["1"], set())
    assert metrics([], {"1"})["ndcg@10"] == 0
    with pytest.raises(BenchmarkError):
        macro([])
    with pytest.raises(ValidationError):
        RankedResult(query_id="q", case_id="1", rank=1, score=float("inf"), run_id="r")


def test_frozen_public_bm25_parity() -> None:
    """Exact scores from the original implementation, not the extracted core."""
    baseline = json.loads(
        (Path(__file__).parents[1] / "fixtures/bm25_public_baseline.json").read_text()
    )
    cases = [case(identifier, *texts) for identifier, texts in baseline["corpus"]]
    for search in baseline["searches"]:
        config = BM25Config(k1=search["k1"], b=search["b"])
        for documents in (cases, list(reversed(cases))):
            results = BM25(documents, config).rank(
                "q",
                search["query"],
                "r",
                case_ids=None if search["ids"] is None else set(search["ids"]),
                top_k=search["limit"],
            )
            assert [[r.case_id, r.score] for r in results] == search["expected"]
            assert [r.rank for r in results] == list(range(1, len(results) + 1))
            assert all(
                r.query_id == "q" and r.run_id == "r" and r.method == "bm25"
                for r in results
            )


def test_positive_numeric_ties() -> None:
    results = BM25([case("10", "apple"), case("2", "apple")]).rank("q", "apple", "r")
    assert [r.case_id for r in results] == ["2", "10"]
    assert results[0].score == results[1].score > 0


@pytest.mark.parametrize("limit", [0, -1])
def test_public_invalid_limit(limit: int) -> None:
    with pytest.raises(BenchmarkError, match="top_k must be positive"):
        BM25([case("2", "apple")]).rank("q", "apple", "r", case_ids=set(), top_k=limit)


def test_opaque_ids_and_filters() -> None:
    documents = [("passage:z/β", "Apple apple"), ("passage:a/α", "Apple apple")]
    expected = BM25Core(documents).search("APPLE")
    assert [identifier for identifier, _ in expected] == ["passage:a/α", "passage:z/β"]
    assert expected[0][1] == expected[1][1] > 0
    assert BM25Core(list(reversed(documents))).search("APPLE") == expected
    core = BM25Core(documents)
    assert core.search("APPLE", top_k=1) == expected[:1]
    assert core.search("APPLE", document_ids={"passage:z/β"}, top_k=1) == expected[1:]
    assert core.search("APPLE", document_ids={"missing"}) == []
    assert core.search("APPLE", document_ids=set()) == []
    assert core.search("") == [(identifier, 0) for identifier, _ in expected]
    assert core.search("!!!") == core.search("unknown") == core.search("")
    order = {"passage:z/β": 0, "passage:a/α": 1}
    assert BM25Core(documents, tie_breaker=lambda key: order[key]).search(
        "APPLE"
    ) == list(reversed(expected))


@pytest.mark.parametrize(
    "documents",
    [[], [("x", "apple"), ("x", "pear")], [("", "apple")], [("x", "!!!")]],
)
def test_invalid_core_corpus(documents: list[tuple[str, str]]) -> None:
    with pytest.raises(BenchmarkError):
        BM25Core(documents)

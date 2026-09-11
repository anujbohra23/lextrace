"""Retrieval contracts and source-preserving lexical search."""

from datetime import date

import pytest
from pydantic import ValidationError

from lextrace.domain.case import Case
from lextrace.evaluation.benchmark import BenchmarkConfig, BenchmarkInputs
from lextrace.retrieval.contracts import SearchFilters, SearchRequest
from lextrace.retrieval.documents import Corpus
from lextrace.retrieval.engine import LexTraceRetriever
from lextrace.retrieval.passages import segment, select
from lextrace.retrieval.settings import PassageSettings

Sample = tuple[list[Case], BenchmarkInputs, BenchmarkConfig]


def test_corpus_identity_and_multiple_opinions(benchmark_sample: Sample) -> None:
    cases, _, _ = benchmark_sample
    first = cases[0].model_copy(
        update={"opinions": [cases[0].opinions[0], cases[1].opinions[0]]}
    )
    corpus = Corpus([first, cases[2]])
    assert corpus.hash == Corpus([cases[2], first]).hash
    assert corpus.text("1") == "\n\n".join(op.text for op in first.opinions)
    assert corpus.ids == ("1", "3")
    changed = first.model_copy(update={"name": "Changed"})
    assert Corpus([changed, cases[2]]).hash != corpus.hash


def test_filters(benchmark_sample: Sample) -> None:
    cases, _, _ = benchmark_sample
    corpus = Corpus(cases[:10])
    filters = SearchFilters(
        courts=["ca2"], filed_after=date(1998, 1, 1), filed_before=date(2000, 1, 1)
    )
    assert corpus.eligible(filters) == {"3", "5"}
    assert corpus.eligible(SearchFilters(courts=["absent"])) == set()
    missing = cases[0].model_copy(update={"date_filed": None})
    assert not Corpus([missing]).eligible(SearchFilters(filed_before=date(2020, 1, 1)))


@pytest.mark.parametrize(
    "data",
    [
        {"filed_after": "2020-01-01", "filed_before": "2000-01-01"},
        {"courts": ["ca2;bad"]},
    ],
)
def test_invalid_filters(data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SearchFilters.model_validate(data)


@pytest.mark.parametrize(
    "data",
    [
        {"query": ""},
        {"query": "x", "top_k": 0},
        {"query": "x", "mode": "unknown"},
        {"query": "x", "top_k": True},
    ],
)
def test_invalid_request(data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SearchRequest.model_validate(data)


def test_passages_cover_text_and_offsets(benchmark_sample: Sample) -> None:
    cases, _, _ = benchmark_sample
    text = "First paragraph with wages.\n\n" + " ".join(["salary"] * 45)
    case = cases[0].model_copy(
        update={
            "opinions": [
                cases[0].opinions[0].model_copy(update={"text": text}),
                cases[1].opinions[0],
            ]
        }
    )
    passages = segment(case, PassageSettings(words=16))
    assert passages == segment(case, PassageSettings(words=16))
    assert len({p.passage_id for p in passages}) == len(passages)
    opinions = {op.source_id: op for op in case.opinions}
    for p in passages:
        assert p.text == opinions[p.opinion_id].text[p.start : p.end]
        assert len(p.text.split()) <= 16
    assert "salary" in select(passages, "salary", {}, 1)[0].text
    assert sum(len(p.text.split()) for p in passages) == sum(
        len(o.text.split()) for o in case.opinions
    )


def test_bm25_engine_diagnostics(
    benchmark_sample: Sample, caplog: pytest.LogCaptureFixture
) -> None:
    cases, _, _ = benchmark_sample
    engine = LexTraceRetriever(Corpus(cases[:10]), mode="bm25")
    with caplog.at_level("INFO", logger="lextrace.retrieval"):
        response = engine.search_response(
            SearchRequest(
                query="private-user-query",
                mode="bm25",
                top_k=3,
                filters=SearchFilters(courts=["ca2"]),
            )
        )
    assert len(response.results) == 3
    assert all(r.court == "ca2" for r in response.results)
    assert len({r.case_id for r in response.results}) == 3
    assert all(r.diagnostics.bm25_score is not None for r in response.results)
    assert response.results == engine.search(
        "private-user-query", 3, SearchFilters(courts=["ca2"])
    )
    assert "private-user-query" not in caplog.text
    assert response.trace.stage_seconds["total"] >= response.trace.stage_seconds["bm25"]

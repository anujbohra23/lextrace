"""Frozen CourtListener metadata adapters; no network or token required."""

import json

import pytest

from lextrace.ingestion.benchmark_sources import (
    parse_citation_page,
    parse_opinion_metadata,
)
from lextrace.ingestion.courtlistener import IngestionError


def test_opinion_and_cluster_ids_are_distinct() -> None:
    data = parse_opinion_metadata(
        json.dumps(
            {
                "id": 99,
                "cluster": "https://www.courtlistener.com/api/rest/v4/clusters/42/",
                "opinions_cited": [
                    "/api/rest/v4/opinions/7/",
                    "/api/rest/v4/opinions/7/",
                ],
            }
        ).encode()
    )
    assert data.id == 99
    assert data.cluster_id == "42"
    assert data.cited_opinion_ids == ["7"]


@pytest.mark.parametrize(
    "link",
    [
        "https://evil.example/api/rest/v4/clusters/42/",
        "//www.courtlistener.com/api/rest/v4/clusters/42/",
        "/api/rest/v4/opinions/42/",
        "/api/rest/v4/clusters/0/",
        "/api/rest/v4/clusters/42/?token=secret",
    ],
)
def test_invalid_metadata_link(link: str) -> None:
    with pytest.raises(IngestionError) as error:
        parse_opinion_metadata(
            json.dumps({"id": 99, "cluster": link, "opinions_cited": []}).encode()
        )
    assert "secret" not in str(error.value)


def test_missing_citations_not_treated_as_empty() -> None:
    with pytest.raises(IngestionError):
        parse_opinion_metadata(b'{"id":99,"cluster":"/api/rest/v4/clusters/42/"}')


def test_incomplete_page_and_depth() -> None:
    page = parse_citation_page(
        json.dumps(
            {
                "results": [
                    {
                        "citing_opinion": "/api/rest/v4/opinions/99/",
                        "cited_opinion": "/api/rest/v4/opinions/7/",
                        "depth": 4,
                    }
                ],
                "next": "https://www.courtlistener.com/api/rest/v4/opinions-cited/?cursor=x",
            }
        ).encode(),
        "99",
    )
    assert page.next is not None
    assert page.results[0].depth == 4
    with pytest.raises(IngestionError, match="unexpected"):
        parse_citation_page(json.dumps(page.model_dump()).encode(), "42")

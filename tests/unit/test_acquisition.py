"""Acquisition uses mocked HTTPX transport exclusively."""

import json
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest

from lextrace.config import COURTLISTENER_API_BASE_URL as BASE
from lextrace.ingestion.benchmark_acquisition import (
    Acquisition,
    SearchPage,
    pagination,
    parse,
)
from lextrace.ingestion.courtlistener import IngestionError, RequestFailure


def hit(identifier: int = 2) -> dict[str, object]:
    return {
        "cluster_id": identifier,
        "court_id": "ca2",
        "dateFiled": "2010-01-01",
        "caseName": "Example",
        "opinions": [{"id": 99, "type": "combined-opinion"}],
    }


def test_search_schema() -> None:
    page = parse(json.dumps({"results": [hit()], "next": None}).encode(), SearchPage)
    assert page.results[0].opinions[0].id != page.results[0].cluster_id
    with pytest.raises(IngestionError):
        parse(b'{"results":[{}],"next":null}', SearchPage)


def test_search_pagination_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    requests = []

    def handler(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        assert req.url.host == "www.courtlistener.com"
        assert req.extensions["timeout"]["read"] == 90
        params = dict(req.url.params)
        assert params["stat_Published"] == "on" and "semantic" not in params
        if "cursor" in params:
            return httpx.Response(200, json={"results": [hit(1)], "next": None})
        return httpx.Response(
            200,
            json={
                "results": [hit()],
                "next": BASE + "search/?" + urlencode({**params, "cursor": "second"}),
            },
        )

    a = Acquisition(
        "secret",
        Path("data/cache"),
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )
    try:
        assert a.discover("ca2", date(2010, 1, 1), date(2010, 1, 1)) == [1, 2]
        assert a.discover("ca2", date(2010, 1, 1), date(2010, 1, 1)) == [1, 2]
        assert len(requests) == 2 and a.cache_hits == 2
        cached = next(Path("data/cache").glob("*.sha256"))
        cached.write_text("broken")
        with pytest.raises(IngestionError, match="hash mismatch"):
            a.discover("ca2", date(2010, 1, 1), date(2010, 1, 1))
    finally:
        a.close()


@pytest.mark.parametrize(
    "link",
    [
        "https://evil.test/api/rest/v4/search/?cursor=x",
        BASE + "search/?cursor=x&q=changed",
        BASE + "opinions/?cursor=x",
        BASE + "search/?cursor=x&cursor=y",
    ],
)
def test_bad_pagination(link: str) -> None:
    with pytest.raises(RequestFailure):
        pagination(link, "search/", {})


def test_fallback_and_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    paths = []

    def handler(req: httpx.Request) -> httpx.Response:
        paths.append(req.url.path)
        if req.url.path.endswith("search/"):
            raise httpx.ReadTimeout("secret", request=req)
        assert req.url.params["date_filed"] == "2010-01-01"
        assert "sub_opinions" in req.url.params["fields"]
        return httpx.Response(200, json={"results": [], "next": None})

    a = Acquisition(
        "secret",
        Path("data/cache"),
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )
    try:
        assert a.discover("ca2", date(2010, 1, 1), date(2010, 1, 1)) == []
    finally:
        a.close()
    assert len(paths) == 2


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 302])
def test_safe_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.chdir(tmp_path)
    a = Acquisition(
        "secret",
        Path("data/cache"),
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                status,
                text="secret",
                headers={"Retry-After": "15", "Location": "https://evil.test"},
            )
        ),
        sleep=lambda _: None,
    )
    try:
        with pytest.raises(RequestFailure) as exc:
            a.get("opinions/1/")
        assert "secret" not in str(exc.value)
        if status == 429:
            assert "15 seconds" in str(exc.value)
        assert a.requests == 1
        assert "secret" not in (Path("data/cache") / "last-run.json").read_text()
    finally:
        a.close()


def test_citation_pagination_depth_zero_and_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    edge = {
        "citing_opinion": BASE + "opinions/1/",
        "cited_opinion": BASE + "opinions/9/",
        "depth": 0,
    }

    def handler(req: httpx.Request) -> httpx.Response:
        params = dict(req.url.params)
        return httpx.Response(
            200,
            json={
                "results": [edge],
                "next": None
                if "cursor" in params
                else BASE + "opinions-cited/?" + urlencode({**params, "cursor": "x"}),
            },
        )

    a = Acquisition(
        "secret",
        Path("data/cache"),
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )
    try:
        edges, hashes, total = a.citations("1")
        assert (
            len(edges) == 1 and edges[0].depth == 0 and len(hashes) == 2 and total == 2
        )
    finally:
        a.close()


def test_repeated_cursor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    a = Acquisition(
        "secret",
        Path("data/cache"),
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={
                    "results": [],
                    "next": BASE
                    + "opinions-cited/?citing_opinion=1&order_by=id&cursor=x",
                },
            )
        ),
        sleep=lambda _: None,
    )
    try:
        with pytest.raises(RequestFailure, match="Repeated"):
            a.citations("1")
    finally:
        a.close()


def test_mapping_parent_and_field_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    def handler(req: httpx.Request) -> httpx.Response:
        assert "fields" in req.url.params
        if "/opinions/" in req.url.path:
            return httpx.Response(
                200,
                json={
                    "id": 99,
                    "type": "020lead",
                    "cluster": BASE + "clusters/42/",
                    "opinions_cited": [],
                    "plain_text": "Usable source text",
                },
            )
        if "/clusters/" in req.url.path:
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "absolute_url": "/opinion/42/test/",
                    "case_name": "Case",
                    "date_filed": "2009-01-01",
                    "docket": BASE + "dockets/7/",
                    "sub_opinions": [BASE + "opinions/99/"],
                },
            )
        return httpx.Response(
            200, json={"id": 7, "court_id": "ca2", "docket_number": "123"}
        )

    a = Acquisition(
        "secret",
        Path("data/cache"),
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )
    try:
        case = a.case("42")
        assert case.source_id == "42" and case.opinions[0].source_id == "99"
        assert a.case("42") == case and a.requests == 3
        with pytest.raises(IngestionError):
            a.opinion("100")
    finally:
        a.close()


def test_budget_no_refetch_on_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"id": 1}))
    a = Acquisition(
        "secret",
        Path("data/cache"),
        max_requests=1,
        transport=transport,
        sleep=lambda _: None,
    )
    try:
        a.get("opinions/1/")
        with pytest.raises(RequestFailure, match="budget"):
            a.get("opinions/2/")
    finally:
        a.close()
    b = Acquisition(
        "secret",
        Path("data/cache"),
        max_requests=1,
        transport=transport,
        sleep=lambda _: None,
    )
    try:
        b.get("opinions/1/")
        assert b.requests == 0
        b.get("opinions/2/")
        assert b.requests == 1
    finally:
        b.close()


@pytest.mark.parametrize("depth", [-1, True, "3"])
def test_invalid_depth(depth: object) -> None:
    from lextrace.ingestion.benchmark_sources import parse_citation_page

    with pytest.raises(IngestionError):
        parse_citation_page(
            json.dumps(
                {
                    "results": [
                        {
                            "citing_opinion": BASE + "opinions/1/",
                            "cited_opinion": BASE + "opinions/2/",
                            "depth": depth,
                        }
                    ],
                    "next": None,
                }
            ).encode(),
            "1",
        )


def test_network_failure_no_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret", request=req)

    a = Acquisition(
        "secret",
        Path("data/cache"),
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )
    try:
        with pytest.raises(RequestFailure, match="connect"):
            a.discover("ca2", date(2010, 1, 1), date(2010, 1, 1))
        assert a.requests == 1
    finally:
        a.close()


def test_job_request_failure_preserves_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lextrace.ingestion.benchmark_job import acquire_benchmark

    monkeypatch.chdir(tmp_path)
    # Quota failure is immediate; no sleep or unrelated requests.
    result = acquire_benchmark(
        Path("data/benchmarks/v1/acquire"),
        "secret",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(429, headers={"Retry-After": "30"})
        ),
    )
    assert result["status"] == "incomplete"
    progress = Path("data/benchmarks/v1/acquire/progress.json").read_text()
    assert "secret" not in progress and "30 seconds" in progress
    assert Path("data/benchmarks/v1/acquire/inputs.json").exists()

"""Provider schema and request boundary tests."""

import httpx
import pytest
from pydantic import ValidationError

from lextrace.ingestion.courtlistener import (
    ClusterResponse,
    IngestionError,
    OpinionResponse,
    fetch_case,
    linked_id,
)


@pytest.mark.parametrize(
    "link",
    [
        "https://evil.test/api/rest/v4/dockets/2/",
        "//www.courtlistener.com/api/rest/v4/dockets/2/",
        "http://www.courtlistener.com/api/rest/v4/dockets/2/",
        "/api/rest/v4/opinions/2/",
        "/api/rest/v4/dockets/0/",
        "/api/rest/v4/dockets/2/?x=1",
        "/api/rest/v4/dockets/2/#x",
        "/api/rest/v4/dockets/../2/",
        "/api/rest/v4/dockets/%32/",
    ],
)
def test_invalid_links(link: str) -> None:
    with pytest.raises(IngestionError):
        linked_id(link, "dockets")


def test_valid_relative_link() -> None:
    assert linked_id("/api/rest/v4/dockets/2/", "dockets") == 2


@pytest.mark.parametrize("identifier", [0, -1, True, "1"])
def test_schema_rejects_invalid_id(identifier: object) -> None:
    with pytest.raises(ValidationError):
        OpinionResponse.model_validate({"id": identifier})


def test_schema_missing_fields() -> None:
    with pytest.raises(ValidationError):
        ClusterResponse.model_validate({"id": 1})


def test_schema_allows_unknown_fields() -> None:
    assert OpinionResponse.model_validate({"id": 1, "future_field": 42}).type is None


def test_unsafe_link_never_requested() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": 1,
                "absolute_url": "/opinion/1/a/",
                "case_name": "A",
                "docket": "https://evil.test/api/rest/v4/dockets/2/",
                "sub_opinions": ["/api/rest/v4/opinions/3/"],
            },
        )

    with pytest.raises(IngestionError):
        fetch_case(1, "fake-token", transport=httpx.MockTransport(handler))
    assert len(requests) == 1
    assert requests[0].url.host == "www.courtlistener.com"


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("120", "120 seconds"),
        ("Wed, 21 Oct 2015 07:28:00 GMT", "2015-10-21T07:28:00+00:00"),
        ("synthetic-secret", "try again later."),
        ("-1", "try again later."),
    ],
)
def test_safe_retry_after(header: str, expected: str) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(429, headers={"Retry-After": header})
    )
    with pytest.raises(IngestionError) as error:
        fetch_case(1, "synthetic-secret", transport=transport)
    assert expected in str(error.value)
    assert "synthetic-secret" not in str(error.value)

"""CourtListener response schemas and synchronous, fixed-origin fetching."""

import re
from datetime import UTC, date
from email.utils import parsedate_to_datetime
from typing import Annotated, Literal, TypeVar
from urllib.parse import parse_qsl, urlsplit

import httpx
from pydantic import BaseModel, Field, ValidationError

from lextrace.config import COURTLISTENER_API_BASE_URL, COURTLISTENER_TIMEOUT

PositiveId = Annotated[int, Field(strict=True, gt=0)]


class IngestionError(Exception):
    """A safe, user-facing ingestion failure without external response details."""


class RequestFailure(IngestionError):
    """A request-level failure that stops corpus ingestion."""


class ReporterCitationResponse(BaseModel):
    volume: str | None = None
    reporter: str
    page: str | None = None


class ClusterPage(BaseModel):
    # Validate records individually so one bad record cannot discard a page.
    results: list[object]
    next: str | None


class ClusterResponse(BaseModel):
    id: PositiveId
    absolute_url: str
    case_name: str
    date_filed: date | None = None
    citations: list[ReporterCitationResponse] | None = None
    docket: str
    sub_opinions: Annotated[list[str], Field(min_length=1)]


class DocketResponse(BaseModel):
    id: PositiveId
    court_id: str
    docket_number: str | None = None


class OpinionResponse(BaseModel):
    id: PositiveId
    type: str | None = None
    html_with_citations: str | None = None
    plain_text: str | None = None


ResponseModel = TypeVar(
    "ResponseModel", ClusterResponse, DocketResponse, OpinionResponse
)


def linked_id(link: str, resource: Literal["dockets", "opinions"]) -> int:
    """Accept only canonical CourtListener resource links; never request them."""
    base = urlsplit(COURTLISTENER_API_BASE_URL)
    try:
        parsed = urlsplit(link)
    except ValueError:
        raise IngestionError(
            "CourtListener returned an invalid resource link."
        ) from None
    if (
        re.search(r"[\\\s]", link)
        or parsed.query
        or parsed.fragment
        or (
            parsed.netloc
            and (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc)
        )
        or (parsed.scheme and not parsed.netloc)
        or link.startswith("//")
    ):
        raise IngestionError("CourtListener returned an invalid resource link.")
    match = re.fullmatch(
        rf"{re.escape(base.path)}{resource}/([1-9][0-9]*)/", parsed.path
    )
    if match is None:
        raise IngestionError("CourtListener returned an invalid resource link.")
    return int(match[1])


def _retry_after(value: str | None) -> str:
    if not value:
        return ""
    if value.isascii() and value.isdecimal() and len(value) <= 10:
        return f" Retry after {int(value)} seconds."
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            return ""
        return f" Retry after {parsed.astimezone(UTC).isoformat()}."
    except (ValueError, TypeError, OverflowError):
        return ""


def request(
    client: httpx.Client, path: str, params: dict[str, str] | None = None
) -> httpx.Response:
    """Request only a caller-constructed API path; never follow redirects."""
    try:
        response = client.get(f"{COURTLISTENER_API_BASE_URL}{path}", params=params)
    except httpx.TimeoutException:
        raise RequestFailure("CourtListener request timed out.") from None
    except httpx.RequestError:
        raise RequestFailure("Could not connect to CourtListener.") from None
    status = response.status_code
    if status in (401, 403):
        raise RequestFailure("CourtListener authentication or access denied (401/403).")
    if status == 404:
        raise RequestFailure("CourtListener resource not found (404).")
    if status == 429:
        raise RequestFailure(
            "CourtListener rate limit reached (429); try again later."
            + _retry_after(response.headers.get("Retry-After"))
        )
    if status >= 500:
        raise RequestFailure("CourtListener server error; try again later.")
    if status != 200:
        raise RequestFailure("CourtListener returned an unexpected HTTP status.")
    return response


def _fetch(
    client: httpx.Client, resource: str, identifier: int, schema: type[ResponseModel]
) -> ResponseModel:
    response = request(client, f"{resource}/{identifier}/")
    try:
        result = schema.model_validate_json(response.content)
    except ValidationError:
        raise IngestionError(
            "CourtListener returned malformed JSON or invalid data."
        ) from None
    if result.id != identifier:
        raise IngestionError(
            "CourtListener returned an unexpected resource identifier."
        )
    return result


def fetch_case(
    cluster_id: int, token: str, *, transport: httpx.BaseTransport | None = None
) -> tuple[ClusterResponse, DocketResponse, list[OpinionResponse]]:
    """Fetch a cluster and its docket/opinions, without retries or redirects."""
    with httpx.Client(
        headers={"Authorization": f"Token {token}"},
        timeout=COURTLISTENER_TIMEOUT,
        follow_redirects=False,
        transport=transport,
    ) as client:
        cluster = _fetch(client, "clusters", cluster_id, ClusterResponse)
        return fetch_cluster_details(client, cluster)


def fetch_cluster_details(
    client: httpx.Client, cluster: ClusterResponse
) -> tuple[ClusterResponse, DocketResponse, list[OpinionResponse]]:
    """Reuse validated list metadata and a shared client for corpus ingestion."""
    docket_id = linked_id(cluster.docket, "dockets")
    opinion_ids = [linked_id(link, "opinions") for link in cluster.sub_opinions]
    docket = _fetch(client, "dockets", docket_id, DocketResponse)
    opinions = [
        _fetch(client, "opinions", identifier, OpinionResponse)
        for identifier in opinion_ids
    ]
    return cluster, docket, opinions


def fetch_page(client: httpx.Client, params: dict[str, str]) -> ClusterPage:
    response = request(client, "clusters/", params)
    try:
        return ClusterPage.model_validate_json(response.content)
    except ValidationError:
        raise RequestFailure(
            "CourtListener returned an invalid cluster page."
        ) from None


def next_cursor(link: str, filters: dict[str, str]) -> str:
    """Validate pagination URL, then return only its opaque cursor."""
    base = urlsplit(COURTLISTENER_API_BASE_URL)
    try:
        parsed = urlsplit(link)
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise RequestFailure("CourtListener returned invalid pagination.") from None
    params = dict(pairs)
    if (
        parsed.scheme != base.scheme
        or parsed.netloc != base.netloc
        or parsed.path != base.path + "clusters/"
        or parsed.fragment
        or re.search(r"[\\\s]", link)
        or len(params) != len(pairs)
        or set(params) != set(filters) | {"cursor"}
        or any(params.get(key) != value for key, value in filters.items())
        or not params.get("cursor")
    ):
        raise RequestFailure("CourtListener returned invalid pagination.")
    return params["cursor"]

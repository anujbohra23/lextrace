"""CourtListener response schemas and synchronous, fixed-origin fetching."""

import re
from datetime import date
from typing import Annotated, Literal, TypeVar
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field, ValidationError

from lextrace.config import COURTLISTENER_API_BASE_URL, COURTLISTENER_TIMEOUT

PositiveId = Annotated[int, Field(strict=True, gt=0)]


class IngestionError(Exception):
    """A safe, user-facing ingestion failure without external response details."""


class ClusterResponse(BaseModel):
    id: PositiveId
    absolute_url: str
    case_name: str
    date_filed: date | None = None
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


def _fetch(
    client: httpx.Client, resource: str, identifier: int, schema: type[ResponseModel]
) -> ResponseModel:
    try:
        response = client.get(f"{COURTLISTENER_API_BASE_URL}{resource}/{identifier}/")
    except httpx.TimeoutException:
        raise IngestionError("CourtListener request timed out.") from None
    except httpx.RequestError:
        raise IngestionError("Could not connect to CourtListener.") from None
    status = response.status_code
    if status in (401, 403):
        raise IngestionError("CourtListener authentication or access denied (401/403).")
    if status == 404:
        raise IngestionError("CourtListener resource not found (404).")
    if status == 429:
        raise IngestionError("CourtListener rate limit reached (429); try again later.")
    if status >= 500:
        raise IngestionError("CourtListener server error; try again later.")
    if status != 200:
        raise IngestionError("CourtListener returned an unexpected HTTP status.")
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
        docket_id = linked_id(cluster.docket, "dockets")
        opinion_ids = [linked_id(link, "opinions") for link in cluster.sub_opinions]
        docket = _fetch(client, "dockets", docket_id, DocketResponse)
        opinions = [
            _fetch(client, "opinions", identifier, OpinionResponse)
            for identifier in opinion_ids
        ]
    return cluster, docket, opinions

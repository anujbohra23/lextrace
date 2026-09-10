"""Offline adapters for frozen CourtListener citation metadata; no acquisition."""

import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ValidationError

from lextrace.config import COURTLISTENER_API_BASE_URL
from lextrace.ingestion.courtlistener import IngestionError, PositiveId


class OpinionMetadataResponse(BaseModel):
    id: PositiveId
    cluster: str
    opinions_cited: list[str]

    @property
    def cluster_id(self) -> str:
        return resource_id(self.cluster, "clusters")

    @property
    def cited_opinion_ids(self) -> list[str]:
        return sorted(
            {resource_id(link, "opinions") for link in self.opinions_cited}, key=int
        )


class CitationRelationResponse(BaseModel):
    citing_opinion: str
    cited_opinion: str
    depth: PositiveId


class CitationPageResponse(BaseModel):
    results: list[CitationRelationResponse]
    next: str | None


def resource_id(link: str, resource: Literal["opinions", "clusters"]) -> str:
    base = urlsplit(COURTLISTENER_API_BASE_URL)
    try:
        parsed = urlsplit(link)
    except ValueError:
        raise IngestionError("Invalid benchmark citation resource link.") from None
    if (
        re.search(r"[\\\s]", link)
        or link.startswith("//")
        or parsed.query
        or parsed.fragment
        or (parsed.scheme, parsed.netloc) not in {("", ""), (base.scheme, base.netloc)}
    ):
        raise IngestionError("Invalid benchmark citation resource link.")
    match = re.fullmatch(
        re.escape(base.path) + resource + r"/([1-9][0-9]*)/", parsed.path
    )
    if match is None:
        raise IngestionError("Invalid benchmark citation resource link.")
    return match[1]


def parse_opinion_metadata(payload: bytes) -> OpinionMetadataResponse:
    try:
        result = OpinionMetadataResponse.model_validate_json(payload)
    except ValidationError:
        raise IngestionError("Invalid benchmark opinion metadata.") from None
    # Validate all references now, not only when consumers access properties.
    _ = result.cluster_id, result.cited_opinion_ids
    return result


def parse_citation_page(payload: bytes, citing_opinion_id: str) -> CitationPageResponse:
    try:
        result = CitationPageResponse.model_validate_json(payload)
    except ValidationError:
        raise IngestionError("Invalid benchmark citation page.") from None
    for relation in result.results:
        if resource_id(relation.citing_opinion, "opinions") != citing_opinion_id:
            raise IngestionError("Citation page has an unexpected citing opinion.")
        resource_id(relation.cited_opinion, "opinions")
    # A non-null next means this page alone is NOT complete evidence.
    return result

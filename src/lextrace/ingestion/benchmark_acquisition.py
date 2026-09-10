"""Bounded, cached REST acquisition. Search discovers IDs; REST supplies evidence."""

import json
import math
import re
import time
from collections.abc import Callable, Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import TypeVar
from urllib.parse import parse_qsl, urlsplit

import httpx
from pydantic import BaseModel, ValidationError

from lextrace.config import COURTLISTENER_API_BASE_URL
from lextrace.corpus import atomic_write
from lextrace.domain.case import Case
from lextrace.evaluation.benchmark import digest
from lextrace.ingestion.benchmark_sources import (
    CitationRelationResponse,
    OpinionMetadataResponse,
    parse_citation_page,
    parse_opinion_metadata,
    resource_id,
)
from lextrace.ingestion.courtlistener import (
    ClusterPage,
    ClusterResponse,
    DocketResponse,
    IngestionError,
    OpinionResponse,
    PositiveId,
    RequestFailure,
    linked_id,
    request,
)
from lextrace.ingestion.normalize import normalize_case

T = TypeVar("T", bound=BaseModel)
CLUSTER_FIELDS = "id,absolute_url,case_name,date_filed,citations,docket,sub_opinions"
OPINION_FIELDS = "id,type,cluster,opinions_cited,html_with_citations,plain_text"


class SearchOpinion(BaseModel):
    id: PositiveId
    type: str


class SearchHit(BaseModel):
    cluster_id: PositiveId
    court_id: str
    dateFiled: date
    caseName: str
    opinions: list[SearchOpinion]


class SearchPage(BaseModel):
    results: list[SearchHit]
    next: str | None


def parse(payload: bytes, schema: type[T]) -> T:
    try:
        return schema.model_validate_json(payload)
    except ValidationError:
        raise IngestionError("Invalid acquisition response schema.") from None


def pagination(link: str, path: str, filters: dict[str, str]) -> dict[str, str]:
    """Validate origin/path/immutable filters; never request an API-provided URL."""
    base = urlsplit(COURTLISTENER_API_BASE_URL)
    try:
        url = urlsplit(link)
        pairs = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise RequestFailure("Invalid acquisition pagination.") from None
    params = dict(pairs)
    if (
        (url.scheme, url.netloc, url.path)
        != (base.scheme, base.netloc, base.path + path)
        or url.fragment
        or re.search(r"[\\\s]", link)
        or len(pairs) != len(params)
        or any(params.get(key) != value for key, value in filters.items())
        or not set(params) - set(filters) <= {"cursor", "page"}
        or not any(params.get(key) for key in ("cursor", "page"))
    ):
        raise RequestFailure("Invalid acquisition pagination.")
    return params


class UsageWindow(BaseModel):
    scope: str
    remaining: int
    blocked: bool
    window_seconds: int


class UsageResponse(BaseModel):
    current_usage: list[UsageWindow]


class Acquisition:
    """One sequential session; successful responses survive interruption unchanged."""

    def __init__(
        self,
        token: str,
        cache: Path,
        *,
        interval: float = 15,
        timeout: float = 90,
        max_requests: int = 40,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if any(not math.isfinite(x) or x <= 0 for x in (interval, timeout)):
            raise IngestionError("Acquisition interval and timeout must be positive.")
        if max_requests < 1:
            raise IngestionError("Acquisition request budget must be positive.")
        if not cache.resolve().is_relative_to(Path("data").resolve()):
            raise IngestionError("Acquisition cache must be under data/.")
        cache.mkdir(parents=True, exist_ok=True)
        self.cache = cache
        self.client = httpx.Client(
            headers={"Authorization": f"Token {token}"},
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
        )
        self.interval, self.sleep, self.clock = interval, sleep, clock
        self.max_requests = max_requests
        self.requests = 0
        self.cache_hits = 0
        self.last: float | None = None
        self.events: list[dict[str, object]] = []
        self.secret = token.encode()
        self.discovery_metadata: dict[int, tuple[str, date]] = {}

    def close(self) -> None:
        self.client.close()

    def get(self, path: str, params: dict[str, str] | None = None) -> bytes:
        if not re.fullmatch(
            r"(?:search|api-usage|clusters|dockets|opinions|opinions-cited)/(?:[1-9][0-9]*/)?",
            path,
        ):
            raise IngestionError("Invalid acquisition resource path.")
        params = params or {}
        key = digest(json.dumps([path, params], sort_keys=True))
        cached = self.cache / (key + ".json")
        checksum = self.cache / (key + ".sha256")
        if path != "api-usage/" and cached.exists() and checksum.exists():
            body = cached.read_bytes()
            if digest(body) == checksum.read_text():
                self.cache_hits += 1
                return body
            raise IngestionError("Cached resource hash mismatch; inspect local cache.")
        if self.requests >= self.max_requests:
            raise RequestFailure("Acquisition request budget reached; cache preserved.")
        if self.last is not None:
            self.sleep(max(0, self.interval - (self.clock() - self.last)))
        self.last = self.clock()
        self.requests += 1
        event: dict[str, object] = {"path": path, "params": params, "key": key}
        self.events.append(event)
        try:
            response = request(self.client, path, params)
            body = response.content
            if self.secret and self.secret in body:
                raise RequestFailure("Response failed credential safety check.")
            # Cache JSON only. Errors/headers never enter persistent response files.
            try:
                decoded = body.decode("utf-8")
                json.loads(decoded)
            except (ValueError, UnicodeDecodeError):
                raise IngestionError("Invalid acquisition JSON.") from None
            event["status"] = "ok"
            atomic_write(cached, decoded)
            atomic_write(checksum, digest(body))
            return body
        except RequestFailure as exc:
            event["status"] = "request_failure"
            # Only project-defined safe messages are logged, never HTTPX exceptions.
            event["reason"] = str(exc)
            raise
        finally:
            event["seconds"] = round(self.clock() - self.last, 3)
            atomic_write(
                self.cache / "last-run.json", json.dumps(self.events, indent=2)
            )

    def check_quota(self) -> None:
        try:
            payload = self.get("api-usage/")
        except RequestFailure as exc:
            if str(exc) == "CourtListener resource not found (404).":
                return
            raise
        usage = parse(payload, UsageResponse)
        limits = [
            window.remaining
            for window in usage.current_usage
            if window.scope == "user" and window.window_seconds >= 3600
        ]
        if limits:
            remaining = min(limits)
            if remaining <= 0:
                raise RequestFailure(
                    "CourtListener account quota exhausted; cache preserved."
                )
            # Request ceiling is conservative across all currently reported windows.
            self.max_requests = min(self.max_requests, self.requests + remaining)

    def pages(self, path: str, filters: dict[str, str]) -> Iterator[bytes]:
        params = filters
        seen: set[str] = set()
        while True:
            key = json.dumps(params, sort_keys=True)
            if key in seen:
                raise RequestFailure("Repeated acquisition pagination cursor.")
            seen.add(key)
            payload = self.get(path, params)
            page = parse(payload, ClusterPage)
            yield payload
            if page.next is None:
                break
            params = pagination(page.next, path, filters)

    def discover(
        self, court: str, start: date, end: date, *, limit: int | None = None
    ) -> list[int]:
        if court not in {"ca2", "scotus"} or start > end:
            raise IngestionError("Invalid benchmark discovery filters.")
        filters = {
            "type": "o",
            "court": court,
            "filed_after": start.isoformat(),
            "filed_before": end.isoformat(),
            "stat_Published": "on",
            "order_by": "dateFiled asc",
        }
        found: dict[int, date] = {}
        try:
            for payload in self.pages("search/", filters):
                for hit in parse(payload, SearchPage).results:
                    if hit.court_id != court or not start <= hit.dateFiled <= end:
                        raise IngestionError("Search returned out-of-scope metadata.")
                    found[hit.cluster_id] = hit.dateFiled
                    self.discovery_metadata[hit.cluster_id] = (court, hit.dateFiled)
                if limit is not None and len(found) >= limit:
                    break
        except RequestFailure as exc:
            # Never fallback after auth, quota, or request-budget failure.
            if str(exc) not in {
                "CourtListener request timed out.",
                "CourtListener server error; try again later.",
            }:
                raise
            return self.discover_windows(court, start, end, limit=limit)
        ordered = sorted(found, key=lambda key: (found[key], key))
        return ordered if limit is None else ordered[:limit]

    def discover_windows(
        self, court: str, start: date, end: date, *, limit: int | None = None
    ) -> list[int]:
        """One-day REST windows only; no retry of the failed broad join."""
        found: dict[int, date] = {}
        day = start
        while day <= end:
            filters = {
                "docket__court": court,
                "date_filed": day.isoformat(),
                "order_by": "id",
                "fields": CLUSTER_FIELDS,
                "precedential_status": "Published",
            }
            for payload in self.pages("clusters/", filters):
                for raw in parse(payload, ClusterPage).results:
                    try:
                        cluster = ClusterResponse.model_validate(raw)
                    except ValidationError:
                        raise IngestionError("Invalid discovery cluster.") from None
                    if cluster.date_filed != day:
                        raise IngestionError("REST discovery returned unexpected date.")
                    found[cluster.id] = day
                    self.discovery_metadata[cluster.id] = (court, day)
            if limit is not None and len(found) >= limit:
                break
            day += timedelta(days=1)
        ordered = sorted(found, key=lambda key: (found[key], key))
        return ordered if limit is None else ordered[:limit]

    def cluster(self, identifier: str) -> ClusterResponse:
        result = parse(
            self.get(f"clusters/{identifier}/", {"fields": CLUSTER_FIELDS}),
            ClusterResponse,
        )
        if str(result.id) != identifier:
            raise IngestionError("Unexpected cluster identifier.")
        return result

    def opinion(
        self, identifier: str
    ) -> tuple[OpinionMetadataResponse, OpinionResponse, bytes]:
        payload = self.get(f"opinions/{identifier}/", {"fields": OPINION_FIELDS})
        metadata = parse_opinion_metadata(payload)
        if str(metadata.id) != identifier:
            raise IngestionError("Unexpected opinion identifier.")
        return metadata, parse(payload, OpinionResponse), payload

    def case(self, identifier: str) -> Case:
        cluster = self.cluster(identifier)
        docket_id = linked_id(cluster.docket, "dockets")
        docket = parse(
            self.get(f"dockets/{docket_id}/", {"fields": "id,court_id,docket_number"}),
            DocketResponse,
        )
        if docket.id != docket_id:
            raise IngestionError("Unexpected docket identifier.")
        opinions = []
        for link in cluster.sub_opinions:
            metadata, opinion, _ = self.opinion(str(linked_id(link, "opinions")))
            if metadata.cluster_id != identifier:
                raise IngestionError("Opinion parent cluster does not match.")
            opinions.append(opinion)
        return normalize_case(cluster, docket, opinions)

    def citations(
        self, identifier: str
    ) -> tuple[list[CitationRelationResponse], list[str], int]:
        relations: dict[str, CitationRelationResponse] = {}
        hashes = []
        total = 0
        for payload in self.pages(
            "opinions-cited/", {"citing_opinion": identifier, "order_by": "id"}
        ):
            page = parse_citation_page(payload, identifier)
            hashes.append(digest(payload))
            total += len(page.results)
            for edge in page.results:
                cited = resource_id(edge.cited_opinion, "opinions")
                if cited in relations and relations[cited].depth != edge.depth:
                    raise IngestionError("Conflicting duplicate citation depths.")
                relations[cited] = edge
        return [relations[key] for key in sorted(relations, key=int)], hashes, total

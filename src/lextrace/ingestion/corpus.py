"""Sequential bounded CourtListener corpus ingestion."""

import math
import time
from collections.abc import Callable
from pathlib import Path

import httpx
from pydantic import ValidationError

from lextrace.config import COURTLISTENER_TIMEOUT
from lextrace.corpus import (
    CorpusError,
    CorpusQuery,
    IngestionRun,
    Manifest,
    Rejection,
    atomic_write,
    prepare,
    save_manifest,
    serialize_cases,
)
from lextrace.ingestion.courtlistener import (
    ClusterResponse,
    IngestionError,
    RequestFailure,
    fetch_cluster_details,
    fetch_page,
    next_cursor,
)
from lextrace.ingestion.normalize import normalize_case


class _Pacer:
    def __init__(
        self,
        interval: float,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        self.interval = interval
        self.clock = clock
        self.sleep = sleep
        self.last: float | None = None

    def __call__(self, request: httpx.Request) -> None:
        if self.last is not None:
            delay = self.interval - (self.clock() - self.last)
            if delay > 0:
                self.sleep(delay)
        self.last = self.clock()


def ingest_corpus(
    query: CorpusQuery,
    output: Path,
    token: str,
    *,
    request_interval: float,
    resume: bool = False,
    transport: httpx.BaseTransport | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Manifest:
    if not math.isfinite(request_interval) or request_interval <= 0:
        raise CorpusError("Request interval must be a finite positive number.")
    if (
        query.filed_after
        and query.filed_before
        and query.filed_after > query.filed_before
    ):
        raise CorpusError("Filed-date range is reversed.")
    cases, manifest = prepare(output, query, resume)
    run = IngestionRun(request_interval=request_interval, initial_case_count=len(cases))
    manifest.runs.append(run)
    save_manifest(output, manifest)
    saved = {case.source_id for case in cases}
    seen: set[int] = set()
    cursors: set[str] = set()
    filters = query.api_filters()
    params = dict(filters)
    try:
        with httpx.Client(
            headers={"Authorization": f"Token {token}"},
            timeout=COURTLISTENER_TIMEOUT,
            follow_redirects=False,
            transport=transport,
            event_hooks={"request": [_Pacer(request_interval, clock, sleep)]},
        ) as client:
            while len(cases) < query.max_cases:
                page = fetch_page(client, params)
                for raw in page.results:
                    if len(cases) >= query.max_cases:
                        break
                    run.source_records_encountered += 1
                    identifier = raw.get("id") if isinstance(raw, dict) else None
                    safe_id = (
                        str(identifier)
                        if type(identifier) is int and identifier > 0
                        else None
                    )
                    try:
                        cluster = ClusterResponse.model_validate(raw)
                    except ValidationError:
                        run.rejections.append(
                            Rejection(source_id=safe_id, reason="invalid_cluster")
                        )
                        save_manifest(output, manifest)
                        continue
                    if cluster.id in seen or str(cluster.id) in saved:
                        run.skipped_duplicate_records += 1
                        continue
                    seen.add(cluster.id)
                    try:
                        case = normalize_case(*fetch_cluster_details(client, cluster))
                    except RequestFailure:
                        raise
                    except IngestionError:
                        run.rejections.append(
                            Rejection(source_id=str(cluster.id), reason="invalid_case")
                        )
                        save_manifest(output, manifest)
                        continue
                    if (
                        case.court_id != query.court
                        or (
                            query.filed_after is not None
                            and (
                                case.date_filed is None
                                or case.date_filed < query.filed_after
                            )
                        )
                        or (
                            query.filed_before is not None
                            and (
                                case.date_filed is None
                                or case.date_filed > query.filed_before
                            )
                        )
                    ):
                        run.rejections.append(
                            Rejection(
                                source_id=case.source_id, reason="filter_mismatch"
                            )
                        )
                        save_manifest(output, manifest)
                        continue
                    cases.append(case)
                    atomic_write(output, serialize_cases(cases))
                    saved.add(case.source_id)
                    run.successfully_normalized_cases += 1
                    save_manifest(output, manifest)
                if len(cases) >= query.max_cases:
                    break
                if page.next is None:
                    run.status = "exhausted"
                    break
                cursor = next_cursor(page.next, filters)
                if cursor in cursors:
                    raise RequestFailure("CourtListener repeated a pagination cursor.")
                cursors.add(cursor)
                params = {**filters, "cursor": cursor}
            if run.status == "running":
                run.status = "complete"
    except KeyboardInterrupt:
        run.status = "interrupted"
        raise
    except RequestFailure:
        run.status = "failed"
        run.failure_reason = "request_failure"
        raise
    except CorpusError:
        run.status = "failed"
        run.failure_reason = "local_failure"
        raise
    except Exception:
        run.status = "failed"
        run.failure_reason = "unexpected_failure"
        raise
    finally:
        save_manifest(output, manifest)
    return manifest

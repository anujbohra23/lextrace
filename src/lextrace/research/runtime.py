"""SQLite persistence, versioned structured-call cache, and bounded jobs."""

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TypeVar

from pydantic import BaseModel

from lextrace.research.contracts import (
    ResearchError,
    ResearchRequest,
    ResearchResponse,
    Usage,
)
from lextrace.research.llm import Output, StructuredLLM
from lextrace.research.prompts import PromptDefinition

RUN_ID = re.compile(r"^[0-9a-f]{32}$")
Schema = TypeVar("Schema", bound=BaseModel)


class WorkflowRunner(Protocol):
    def run(
        self, request: ResearchRequest, *, run_id: str | None = None
    ) -> ResearchResponse: ...


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: BaseModel) -> str:
    return value.model_dump_json()


class RunStore:
    """Thread-safe local run ownership; the database belongs to runtime only."""

    def __init__(self, path: Path, *, persist_content: bool = True) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.persist_content = persist_content
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._create()

    def _create(self) -> None:
        with self._db:
            self._db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS research_runs (
                    run_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, completed_at TEXT,
                    workflow_version TEXT NOT NULL, question_hash TEXT NOT NULL,
                    question TEXT, request_json TEXT, jurisdiction TEXT,
                    as_of_date TEXT, provider TEXT, model TEXT,
                    grounding_json TEXT, total_latency REAL, input_tokens INTEGER,
                    output_tokens INTEGER, error_code TEXT, result_json TEXT,
                    trace_json TEXT
                );
                CREATE TABLE IF NOT EXISTS node_runs (
                    run_id TEXT NOT NULL, node TEXT NOT NULL, status TEXT NOT NULL,
                    prompt_identity TEXT, model TEXT, input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL, latency REAL NOT NULL,
                    retry_count INTEGER NOT NULL, safe_error TEXT,
                    PRIMARY KEY (run_id, node)
                );
                CREATE TABLE IF NOT EXISTS evidence_refs (
                    run_id TEXT NOT NULL, case_id TEXT NOT NULL,
                    passage_id TEXT NOT NULL, retrieval_trace_id TEXT,
                    PRIMARY KEY (run_id, case_id, passage_id)
                );
                CREATE TABLE IF NOT EXISTS claim_verifications (
                    run_id TEXT NOT NULL, claim_id TEXT NOT NULL, status TEXT NOT NULL,
                    supporting_passage_ids TEXT NOT NULL, support_score REAL NOT NULL,
                    PRIMARY KEY (run_id, claim_id)
                );
                """
            )

    @staticmethod
    def validate_id(run_id: str) -> str:
        if not RUN_ID.fullmatch(run_id):
            raise ResearchError("Research run ID is invalid.")
        return run_id

    def create(self, run_id: str, request: ResearchRequest) -> None:
        self.validate_id(run_id)
        encoded = request.question.encode()
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO research_runs
                (run_id,status,created_at,workflow_version,question_hash,question,
                 request_json,jurisdiction,as_of_date)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    "queued",
                    _now(),
                    "research-v1",
                    hashlib.sha256(encoded).hexdigest(),
                    request.question if self.persist_content else None,
                    _json(request) if self.persist_content else None,
                    request.jurisdiction,
                    request.as_of_date.isoformat() if request.as_of_date else None,
                ),
            )

    def mark_running(self, run_id: str) -> None:
        self._status(run_id, "running")

    def _status(self, run_id: str, status: str, error: str | None = None) -> None:
        self.validate_id(run_id)
        with self._lock, self._db:
            self._db.execute(
                "UPDATE research_runs SET status=?, error_code=? WHERE run_id=?",
                (status, error, run_id),
            )

    def complete(self, response: ResearchResponse) -> None:
        trace = response.trace
        with self._lock, self._db:
            self._db.execute(
                """UPDATE research_runs SET status=?,completed_at=?,provider=?,model=?,
                grounding_json=?,total_latency=?,input_tokens=?,output_tokens=?,
                result_json=?,trace_json=? WHERE run_id=?""",
                (
                    trace.status,
                    _now(),
                    trace.provider,
                    trace.model,
                    _json(response.grounding_summary),
                    trace.total_seconds,
                    trace.usage.input_tokens,
                    trace.usage.output_tokens,
                    _json(response) if self.persist_content else None,
                    _json(trace),
                    response.run_id,
                ),
            )
            for index, node in enumerate(trace.nodes_executed):
                usage = trace.node_usage.get(node, Usage())
                prompt = trace.prompts[index] if index < len(trace.prompts) else None
                self._db.execute(
                    """INSERT OR REPLACE INTO node_runs VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        response.run_id,
                        node,
                        "completed",
                        prompt,
                        trace.model,
                        usage.input_tokens,
                        usage.output_tokens,
                        trace.node_seconds.get(node, 0),
                        trace.retry_count,
                        None,
                    ),
                )
            trace_id = (
                trace.retrieval_trace_ids[0] if trace.retrieval_trace_ids else None
            )
            for item in response.relevant_cases:
                self._db.execute(
                    "INSERT OR REPLACE INTO evidence_refs VALUES (?,?,?,?)",
                    (
                        response.run_id,
                        item.result.case_id,
                        item.result.relevant_passage.passage_id,
                        trace_id,
                    ),
                )
            for result in response.verification_results:
                self._db.execute(
                    "INSERT OR REPLACE INTO claim_verifications VALUES (?,?,?,?,?)",
                    (
                        response.run_id,
                        result.claim_id,
                        result.status,
                        json.dumps(result.supporting_passage_ids, sort_keys=True),
                        result.support_score,
                    ),
                )

    def fail(self, run_id: str, code: str = "workflow_failed") -> None:
        self.validate_id(run_id)
        with self._lock, self._db:
            self._db.execute(
                """UPDATE research_runs SET status='failed',completed_at=?,error_code=?
                WHERE run_id=?""",
                (_now(), code, run_id),
            )

    def get(self, run_id: str) -> dict[str, object] | None:
        self.validate_id(run_id)
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM research_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return dict(row) if row else None

    def response(self, run_id: str) -> ResearchResponse | None:
        row = self.get(run_id)
        value = row.get("result_json") if row else None
        return (
            ResearchResponse.model_validate_json(value)
            if isinstance(value, str)
            else None
        )

    def request(self, run_id: str) -> ResearchRequest | None:
        row = self.get(run_id)
        value = row.get("request_json") if row else None
        return (
            ResearchRequest.model_validate_json(value)
            if isinstance(value, str)
            else None
        )

    def trace(self, run_id: str) -> dict[str, object] | None:
        row = self.get(run_id)
        value = row.get("trace_json") if row else None
        return json.loads(value) if isinstance(value, str) else None

    def list(self, limit: int = 20) -> list[dict[str, object]]:
        with self._lock:
            rows = self._db.execute(
                """SELECT run_id,status,created_at,completed_at,question_hash,
                jurisdiction,as_of_date,provider,model,total_latency,input_tokens,
                output_tokens,error_code FROM research_runs
                ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._db.close()


class StructuredCache:
    """Transactional cache keyed by every representation-affecting input."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        with self._db:
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS structured_cache (
                key TEXT PRIMARY KEY, schema_name TEXT NOT NULL,
                value TEXT NOT NULL, created_at TEXT NOT NULL)"""
            )

    def get(self, key: str, schema: type[Schema]) -> Schema | None:
        with self._lock:
            row = self._db.execute(
                "SELECT value,schema_name FROM structured_cache WHERE key=?", (key,)
            ).fetchone()
        if row is None or row[1] != schema.__name__:
            return None
        try:
            return schema.model_validate_json(row[0])
        except Exception:
            with self._lock, self._db:
                self._db.execute("DELETE FROM structured_cache WHERE key=?", (key,))
            return None

    def put(self, key: str, value: BaseModel) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO structured_cache VALUES (?,?,?,?)",
                (key, value.__class__.__name__, _json(value), _now()),
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()


class CachedStructuredLLM:
    """Cache structured tasks by prompt, model, workflow, schema, and context."""

    provider: str
    model: str

    def __init__(self, inner: StructuredLLM, cache: StructuredCache) -> None:
        self.inner = inner
        self.cache = cache
        self.provider = inner.provider
        self.model = inner.model
        self.usage = Usage()
        self.retries = 0
        self.hits = 0
        self.misses = 0

    def generate(
        self,
        prompt: PromptDefinition,
        schema: type[Output],
        context: dict[str, object],
    ) -> Output:
        identity = json.dumps(
            {
                "workflow": "research-v1",
                "prompt": prompt.identity,
                "provider": self.provider,
                "model": self.model,
                "schema": schema.__name__,
                "context": context,
            },
            default=str,
            sort_keys=True,
            separators=(",", ":"),
        )
        key = hashlib.sha256(identity.encode()).hexdigest()
        cached = self.cache.get(key, schema)
        if cached is not None:
            self.hits += 1
            return cached
        self.misses += 1
        before = self.inner.usage.model_copy()
        retries = self.inner.retries
        result = self.inner.generate(prompt, schema, context)
        self.usage.calls += self.inner.usage.calls - before.calls
        self.usage.input_tokens += self.inner.usage.input_tokens - before.input_tokens
        self.usage.cached_input_tokens += (
            self.inner.usage.cached_input_tokens - before.cached_input_tokens
        )
        self.usage.output_tokens += (
            self.inner.usage.output_tokens - before.output_tokens
        )
        self.retries += self.inner.retries - retries
        self.cache.put(key, result)
        return result


class PricingConfig(BaseModel):
    """Versioned optional per-million-token prices for one exact model ID."""

    version: str
    model: str
    input_per_million: float | None = None
    cached_input_per_million: float | None = None
    output_per_million: float | None = None

    def estimate(self, usage: Usage) -> float | None:
        if self.input_per_million is None or self.output_per_million is None:
            return None
        uncached = max(usage.input_tokens - usage.cached_input_tokens, 0)
        cached_price = self.cached_input_per_million or self.input_per_million
        return (
            uncached * self.input_per_million
            + usage.cached_input_tokens * cached_price
            + usage.output_tokens * self.output_per_million
        ) / 1_000_000


class ResearchJobs:
    """Bounded local executor with durable status and safe manual resume."""

    def __init__(
        self, workflow: WorkflowRunner, store: RunStore, *, max_workers: int = 1
    ) -> None:
        self.workflow = workflow
        self.store = store
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="lextrace-research"
        )
        self._futures: dict[str, Future[None]] = {}
        self._lock = threading.Lock()

    def submit(self, request: ResearchRequest, *, run_id: str | None = None) -> str:
        identifier = run_id or uuid.uuid4().hex
        if run_id is None:
            self.store.create(identifier, request)
        future = self._executor.submit(self._execute, identifier, request)
        with self._lock:
            self._futures[identifier] = future
        return identifier

    def _execute(self, run_id: str, request: ResearchRequest) -> None:
        self.store.mark_running(run_id)
        try:
            self.store.complete(self.workflow.run(request, run_id=run_id))
        except Exception:
            self.store.fail(run_id)

    def resume(self, run_id: str) -> str:
        row = self.store.get(run_id)
        request = self.store.request(run_id)
        if row is None:
            raise ResearchError("Research run was not found.")
        if row["status"] not in {"failed", "degraded"}:
            raise ResearchError("Research run is not resumable.")
        if request is None:
            raise ResearchError("Research content persistence is disabled.")
        self.store._status(run_id, "queued", None)
        return self.submit(request, run_id=run_id)

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=False)

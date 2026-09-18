"""Durable runtime, cache invalidation, corruption, and background jobs."""

import sqlite3
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from lextrace.research.contracts import (
    IssueOutput,
    ResearchRequest,
    ResearchResponse,
    Usage,
)
from lextrace.research.prompts import PromptDefinition, prompt
from lextrace.research.runtime import (
    CachedStructuredLLM,
    ResearchJobs,
    RunStore,
    StructuredCache,
)

Output = TypeVar("Output", bound=BaseModel)


class FakeProvider:
    provider = "fake"
    model = "fake-v1"
    retries = 0

    def __init__(self) -> None:
        self.usage = Usage()

    def generate(
        self,
        definition: PromptDefinition,
        schema: type[Output],
        context: dict[str, object],
    ) -> Output:
        self.usage.calls += 1
        self.usage.input_tokens += 2
        return schema.model_validate({"issues": []})


class FakeWorkflow:
    def __init__(self, response: ResearchResponse, *, fail: bool = False) -> None:
        self.response = response
        self.fail = fail

    def run(
        self, request: ResearchRequest, *, run_id: str | None = None
    ) -> ResearchResponse:
        if self.fail:
            raise RuntimeError("private dependency detail")
        return self.response.model_copy(
            update={
                "run_id": run_id,
                "trace": self.response.trace.model_copy(update={"run_id": run_id}),
            }
        )


def test_run_store_round_trip_and_content_policy(
    tmp_path: Path, empty_research_response: ResearchResponse
) -> None:
    store = RunStore(tmp_path / "runs.sqlite3")
    request = ResearchRequest(question="Private question", jurisdiction="ca2")
    run_id = "a" * 32
    store.create(run_id, request)
    store.mark_running(run_id)
    response = empty_research_response.model_copy(
        update={
            "run_id": run_id,
            "trace": empty_research_response.trace.model_copy(
                update={"run_id": run_id}
            ),
        }
    )
    store.complete(response)
    assert store.response(run_id) == response
    assert store.trace(run_id) is not None
    assert store.list()[0]["question_hash"] != request.question
    store.close()

    private = RunStore(tmp_path / "private.sqlite3", persist_content=False)
    private.create("b" * 32, request)
    row = private.get("b" * 32)
    assert row is not None and row["question"] is None
    assert private.request("b" * 32) is None
    private.close()


def test_cache_hit_version_miss_and_corruption(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite3"
    cache = StructuredCache(path)
    provider = FakeProvider()
    cached = CachedStructuredLLM(provider, cache)
    context: dict[str, object] = {"question": "synthetic"}
    assert cached.generate(prompt("issue-spotting"), IssueOutput, context).issues == []
    assert cached.generate(prompt("issue-spotting"), IssueOutput, context).issues == []
    assert (cached.hits, cached.misses, provider.usage.calls) == (1, 1, 1)
    provider.model = "fake-v2"
    cached.model = provider.model
    cached.generate(prompt("issue-spotting"), IssueOutput, context)
    assert cached.misses == 2
    with sqlite3.connect(path) as database:
        database.execute("UPDATE structured_cache SET value='not-json'")
        database.commit()
    cached.generate(prompt("issue-spotting"), IssueOutput, context)
    assert cached.misses == 3
    cache.close()


def test_background_completion_failure_and_unique_ids(
    tmp_path: Path, empty_research_response: ResearchResponse
) -> None:
    store = RunStore(tmp_path / "runs.sqlite3")
    request = ResearchRequest(question="Synthetic question")
    jobs = ResearchJobs(FakeWorkflow(empty_research_response), store, max_workers=2)
    first = jobs.submit(request)
    second = jobs.submit(request)
    assert first != second
    jobs._futures[first].result(timeout=2)
    jobs._futures[second].result(timeout=2)
    first_row = store.get(first)
    assert first_row is not None and first_row["status"] == "completed"
    jobs.close()

    failed = ResearchJobs(FakeWorkflow(empty_research_response, fail=True), store)
    identifier = failed.submit(request)
    failed._futures[identifier].result(timeout=2)
    failed_row = store.get(identifier)
    assert failed_row is not None and failed_row["status"] == "failed"
    assert "private dependency detail" not in str(failed_row)
    failed.close()
    store.close()


def test_progress_redacts_unlisted_attributes_and_recovers_interruption(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "progress.sqlite3")
    run_id = "c" * 32
    store.create(run_id, ResearchRequest(question="Synthetic question"))
    store.mark_running(run_id)
    store.emit(
        "node.started", {"run_id": run_id, "node": "retrieval", "token": "secret-value"}
    )
    trace = store.trace(run_id)
    assert trace and trace["stage"] == "retrieval"
    assert "secret-value" not in str(trace)
    store.recover_interrupted()
    row = store.get(run_id)
    assert row and row["status"] == "failed" and row["error_code"] == "interrupted"
    store.close()


def test_queue_bound_and_stop_preserve_existing_work(
    tmp_path: Path, empty_research_response: ResearchResponse
) -> None:
    import threading

    import pytest

    from lextrace.research.contracts import ResearchError

    entered, release = threading.Event(), threading.Event()

    class BlockingWorkflow(FakeWorkflow):
        def run(
            self, request: ResearchRequest, *, run_id: str | None = None
        ) -> ResearchResponse:
            entered.set()
            assert release.wait(5)
            return super().run(request, run_id=run_id)

    store = RunStore(tmp_path / "bounded.sqlite3")
    jobs = ResearchJobs(BlockingWorkflow(empty_research_response), store)
    try:
        request = ResearchRequest(question="Synthetic")
        first = jobs.submit(request)
        assert entered.wait(2)
        queued = [jobs.submit(request) for _ in range(3)]
        with pytest.raises(ResearchError, match="queue is full"):
            jobs.submit(request)
        jobs.stop(queued[0])
        assert (store.get(queued[0]) or {})["error_code"] == "cancelled"
        jobs.stop(first)
        assert (store.get(first) or {})["status"] == "stopping"
        release.set()
        jobs._futures[first].result(timeout=2)
        assert (store.get(first) or {})["error_code"] == "cancelled"
    finally:
        release.set()
        jobs.close()
        store.close()

"""Asynchronous research API lifecycle without a live provider."""

from pathlib import Path

from fastapi.testclient import TestClient

from lextrace.api.app import create_app
from lextrace.config import AppSettings
from lextrace.research.contracts import ResearchRequest, ResearchResponse
from lextrace.research.runtime import ResearchJobs, RunStore


class StubWorkflow:
    def __init__(self, response: ResearchResponse) -> None:
        self.response = response

    def run(
        self, request: ResearchRequest, *, run_id: str | None = None
    ) -> ResearchResponse:
        assert request.question == "What authority should be researched?"
        return self.response.model_copy(
            update={
                "run_id": run_id,
                "trace": self.response.trace.model_copy(update={"run_id": run_id}),
            }
        )


def test_research_lifecycle_trace_runs_and_request_id(
    tmp_path: Path, empty_research_response: ResearchResponse
) -> None:
    store = RunStore(tmp_path / "runtime.sqlite3")
    jobs = ResearchJobs(StubWorkflow(empty_research_response), store)
    settings = AppSettings(cors_origins=["http://frontend.test"])
    with TestClient(create_app(research_jobs=jobs, settings=settings)) as client:
        response = client.post(
            "/research",
            json={"question": "What authority should be researched?"},
            headers={"origin": "http://frontend.test", "x-request-id": "request-1"},
        )
        assert response.status_code == 202
        assert response.headers["x-request-id"] == "request-1"
        assert response.headers["access-control-allow-origin"] == "http://frontend.test"
        run_id = response.json()["run_id"]
        jobs._futures[run_id].result(timeout=2)
        result = client.get(f"/research/{run_id}").json()
        assert result["status"] == "completed"
        assert result["result"]["trace"]["provider"] == "fake"
        assert client.get(f"/research/{run_id}/trace").status_code == 200
        assert client.get("/runs").json()[0]["run_id"] == run_id
        assert client.post(f"/research/{run_id}/resume").status_code == 409
        assert client.get("/research/not-a-run").status_code == 400
        assert client.get("/research/" + "f" * 32).status_code == 404
    jobs.close()
    store.close()


def test_research_validation(
    tmp_path: Path, empty_research_response: ResearchResponse
) -> None:
    store = RunStore(tmp_path / "runtime.sqlite3")
    jobs = ResearchJobs(StubWorkflow(empty_research_response), store)
    with TestClient(create_app(research_jobs=jobs)) as client:
        assert client.post("/research", json={"question": ""}).status_code == 422
    jobs.close()
    store.close()

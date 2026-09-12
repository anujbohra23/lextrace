"""Research API tests use an injected workflow and no provider."""

from fastapi.testclient import TestClient

from lextrace.api.app import create_app
from lextrace.research.contracts import ResearchRequest, ResearchResponse


class StubWorkflow:
    def __init__(self, response: ResearchResponse) -> None:
        self.response = response

    def run(self, request: ResearchRequest) -> ResearchResponse:
        assert request.question == "What authority should be researched?"
        return self.response


def test_post_research(empty_research_response: ResearchResponse) -> None:
    workflow = StubWorkflow(empty_research_response)
    with TestClient(create_app(research_workflow=workflow)) as client:
        response = client.post(
            "/research", json={"question": "What authority should be researched?"}
        )
        assert response.status_code == 200
        assert response.json()["trace"]["provider"] == "fake"


def test_research_validation(empty_research_response: ResearchResponse) -> None:
    workflow = StubWorkflow(empty_research_response)
    with TestClient(create_app(research_workflow=workflow)) as client:
        assert client.post("/research", json={"question": ""}).status_code == 422

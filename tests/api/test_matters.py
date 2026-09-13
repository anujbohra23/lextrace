"""Matter API lifecycle, private upload, async analysis, and safe errors."""

import time
from datetime import date
from pathlib import Path
from typing import TypeVar

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, HttpUrl

from lextrace.api.app import create_app
from lextrace.config import AppSettings
from lextrace.domain.case import Case, Opinion
from lextrace.research.contracts import Usage
from lextrace.research.prompts import PromptDefinition
from lextrace.retrieval.contracts import SearchRequest, SearchResponse
from lextrace.retrieval.documents import Corpus
from lextrace.retrieval.engine import LexTraceRetriever

Output = TypeVar("Output", bound=BaseModel)
BRIEF = (
    "A plaintiff must show protected activity and a materially adverse action. "
    "Smith v. Example, 123 F.3d 456."
)


class StubLLM:
    provider = "offline-test"
    model = "deterministic"
    retries = 0

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.usage = Usage()

    def generate(
        self, prompt: PromptDefinition, schema: type[Output], context: dict[str, object]
    ) -> Output:
        self.usage.calls += 1
        if prompt.prompt_id in {"matter-issues", "matter-claims"}:
            sections = context["sections"]
            assert isinstance(sections, list)
            first = sections[0]
            assert isinstance(first, dict)
            identifier = first["section_id"]
            value: object = (
                {"issues": [{"label": "Retaliation", "section_ids": [identifier]}]}
                if prompt.prompt_id == "matter-issues"
                else {
                    "claims": [
                        {
                            "exact_quote": (
                                "A plaintiff must show protected activity and a "
                                "materially adverse action."
                            ),
                            "section_id": identifier,
                            "normalized_proposition": (
                                "Protected activity and adverse action are required."
                            ),
                            "issue_label": "Retaliation",
                        }
                    ]
                }
            )
        elif prompt.prompt_id == "matter-counter":
            value = {"query": "limitations on retaliation claims"}
        else:
            rows = context["evidence"]
            assert isinstance(rows, list)
            value = {
                "judgments": [
                    {
                        "evidence_id": row["evidence_id"],
                        "status": "SUPPORTED",
                        "supporting_passage_ids": [row["passage_id"]],
                        "explanation": "Offline test only.",
                    }
                    for row in rows
                    if isinstance(row, dict)
                ]
            }
        return schema.model_validate(value)


def _settings(tmp_path: Path, *, model: str | None = None) -> AppSettings:
    return AppSettings(
        matter_db=tmp_path / "matters.sqlite3",
        private_matter_root=tmp_path / "private",
        llm_model=model,
    )


def test_matter_lifecycle_and_private_upload(tmp_path: Path) -> None:
    with TestClient(create_app(settings=_settings(tmp_path))) as client:
        matter = client.post("/matters", json={"name": "Smith v. Example"})
        assert matter.status_code == 201
        matter_id = matter.json()["matter_id"]
        assert client.get("/matters").json()[0]["matter_id"] == matter_id
        upload = client.post(
            f"/matters/{matter_id}/documents",
            files={"file": ("motion.txt", BRIEF.encode(), "text/plain")},
        )
        assert upload.status_code == 201
        document_id = upload.json()["document_id"]
        detail = client.get(f"/matters/{matter_id}/documents/{document_id}").json()
        assert detail["text"] == BRIEF
        assert detail["sections"][0]["span"]["start"] == 0
        assert client.get(f"/matters/{matter_id}/claims").json() == []
        accepted = client.post(f"/matters/{matter_id}/documents/{document_id}/analyze")
        assert accepted.status_code == 202
        job_id = accepted.json()["job_id"]
        assert client.get(f"/matters/{matter_id}/documents/../bad").status_code in {
            400,
            404,
        }
        assert (
            client.post(
                f"/matters/{matter_id}/documents",
                files={"file": ("../../secret.txt", b"secret", "text/plain")},
            ).status_code
            == 400
        )
        root = tmp_path / "private" / matter_id
        for _ in range(100):
            job = client.get(f"/matters/{matter_id}/jobs/{job_id}").json()
            if job["status"] == "failed":
                break
            time.sleep(0.01)
        # Missing model is a safe failed background job, never a stack trace.
        assert job["error_code"] == "MODEL_NOT_CONFIGURED"
        assert root.exists()
        assert client.delete(f"/matters/{matter_id}").status_code == 204
        assert not root.exists()
        assert client.get(f"/matters/{matter_id}").status_code == 404


def test_background_xray_and_manual_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib

    api_module = importlib.import_module("lextrace.api.app")
    monkeypatch.setattr(api_module, "OpenAICompatibleLLM", StubLLM)
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    case = Case(
        source_id="1",
        source_url=HttpUrl("https://www.courtlistener.com/opinion/1/example/"),
        name="Smith v. Example",
        date_filed=date(2009, 1, 1),
        court_id="ca2",
        docket_number="1",
        reporter_citations=["123 F.3d 456"],
        opinions=[
            Opinion(
                source_id="11",
                kind="majority",
                text="Protected activity and a materially adverse action are elements.",
                text_source_field="plain_text",
            )
        ],
    )
    engine = LexTraceRetriever(Corpus([case]), mode="bm25")
    original = engine.search_response

    def lexical(request: SearchRequest) -> SearchResponse:
        return original(request.model_copy(update={"mode": "bm25"}))

    monkeypatch.setattr(engine, "search_response", lexical)
    with TestClient(
        create_app(retriever=engine, settings=_settings(tmp_path, model="test"))
    ) as client:
        matter_id = client.post("/matters", json={"name": "Test"}).json()["matter_id"]
        document_id = client.post(
            f"/matters/{matter_id}/documents",
            files={"file": ("brief.txt", BRIEF.encode(), "text/plain")},
        ).json()["document_id"]
        accepted = client.post(f"/matters/{matter_id}/documents/{document_id}/analyze")
        assert accepted.status_code == 202
        job_id = accepted.json()["job_id"]
        for _ in range(100):
            job = client.get(f"/matters/{matter_id}/jobs/{job_id}").json()
            if job["status"] in {"completed", "failed"}:
                break
            time.sleep(0.01)
        assert job["status"] == "completed"
        findings = client.get(f"/matters/{matter_id}/argument-xray").json()
        assert len(findings) == 1
        claim_id = findings[0]["claim_id"]
        assert findings[0]["evidence"][0]["result"]["case_id"] == "1"
        assert client.get(f"/matters/{matter_id}/claims/{claim_id}").json()["finding"]
        cited = findings[0]["cited_authorities"][0]
        review = client.patch(
            f"/matters/{matter_id}/claims/{claim_id}/authorities/1",
            json={"evidence_id": cited["evidence_id"], "removed": True},
        )
        assert review.status_code == 200
        assert review.json()["removed"]
        assert (
            client.get(f"/matters/{matter_id}/argument-xray").json()[0]["vulnerability"]
            == "INSUFFICIENT_EVIDENCE"
        )
        edited = client.patch(
            f"/matters/{matter_id}/claims/{claim_id}",
            json={"normalized_proposition": "Edited by lawyer."},
        )
        assert edited.json()["exact_source_text"].startswith("A plaintiff")
        assert edited.json()["manually_edited"]
        assert client.get(f"/matters/{matter_id}/argument-xray").json() == []

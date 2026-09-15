"""Offline review gate, background run, attack surface, and matrix API."""

import time
from pathlib import Path
from typing import TypeVar

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, HttpUrl

from lextrace.api.app import create_app
from lextrace.config import AppSettings
from lextrace.matter.contracts import (
    ArgumentFinding,
    LegalClaim,
    LegalIssue,
    MatterAnalysis,
    SourceSpan,
)
from lextrace.matter.store import MatterStore
from lextrace.research.contracts import Usage
from lextrace.research.prompts import PromptDefinition
from lextrace.retrieval.contracts import (
    Diagnostics,
    Passage,
    RetrievalResult,
    RetrievalTrace,
    SearchRequest,
    SearchResponse,
)
from lextrace.retrieval.settings import EngineConfig

Output = TypeVar("Output", bound=BaseModel)


class FakeSearch:
    graph = None
    config = EngineConfig()

    def close(self) -> None:
        pass

    def search_response(self, request: SearchRequest) -> SearchResponse:
        result = RetrievalResult(
            case_id="1",
            rank=1,
            final_score=1.0,
            retrieval_method="bm25",
            case_name="Case 1",
            court="ca2",
            date_filed=None,
            reporter_citations=None,
            source_url=HttpUrl("https://www.courtlistener.com/opinion/1/"),
            relevant_passage=Passage(
                passage_id="passage-1",
                opinion_id="opinion-1",
                start=0,
                end=20,
                text="The exact case passage.",
            ),
            diagnostics=Diagnostics(),
        )
        return SearchResponse(
            results=[result],
            trace=RetrievalTrace(
                request_id="offline",
                mode="bm25",
                corpus_hash="offline",
                index_version="test",
                query_length=len(request.query),
            ),
        )


def test_matter_research_review_and_export(tmp_path: Path) -> None:
    settings = AppSettings(
        matter_db=tmp_path / "matter.sqlite3",
        private_matter_root=tmp_path / "private",
    )
    store = MatterStore(settings.matter_db, settings.private_matter_root)
    matter = store.create_matter("Offline matter", court="ca2")
    document = store.add_document(matter.matter_id, "brief.txt", b"The rule applies.")
    span = SourceSpan(
        document_id=document.document_id, section_id="section", start=0, end=17
    )
    claim = LegalClaim(
        claim_id="b" * 32,
        matter_id=matter.matter_id,
        document_id=document.document_id,
        exact_source_text="The rule applies.",
        normalized_proposition="The rule applies.",
        span=span,
    )
    finding = ArgumentFinding(
        claim_id=claim.claim_id,
        issue_id=None,
        proposition=claim.normalized_proposition,
        source=span,
        cited_authorities=[],
        evidence=[],
        counter_authorities=[],
        unresolved_citation_ids=[],
        verification_status="INSUFFICIENT_EVIDENCE",
        research_coverage="NO_RESULTS",
        vulnerability="INSUFFICIENT_EVIDENCE",
        explanation="No verified support.",
    )
    store.save_analysis(
        MatterAnalysis(
            matter_id=matter.matter_id,
            document_id=document.document_id,
            issues=[],
            claims=[claim],
            citations=[],
            findings=[finding],
        )
    )
    store.close()
    search = FakeSearch()
    with TestClient(create_app(retriever=search, settings=settings)) as client:  # type: ignore[arg-type]
        base = f"/matters/{matter.matter_id}/claims/{claim.claim_id}"
        coverage = client.get(f"{base}/research-coverage")
        assert coverage.status_code == 200
        assert coverage.json()["category"] == "UNKNOWN"
        plan = client.post(f"{base}/research-plan")
        assert plan.status_code == 200
        assert plan.json()["status"] == "DRAFT"
        assert client.post(f"{base}/deep-research").status_code == 400
        steps = plan.json()["steps"][:1]
        steps[0]["approved"] = True
        approved = client.patch(
            f"{base}/research-plan", json={"steps": steps, "approve": True}
        )
        assert approved.json()["status"] == "APPROVED"
        accepted = client.post(f"{base}/deep-research")
        assert accepted.status_code == 202
        run_id = accepted.json()["run_id"]
        for _ in range(100):
            run = client.get(f"/matters/{matter.matter_id}/research-runs/{run_id}")
            if run.json()["status"] not in {"queued", "running"}:
                break
            time.sleep(0.01)
        assert run.json()["status"] == "completed"
        assert run.json()["discoveries"][0]["result"]["case_id"] == "1"
        red = client.post(f"{base}/red-team")
        assert red.status_code == 202
        job_id = red.json()["job_id"]
        for _ in range(100):
            job = client.get(f"/matters/{matter.matter_id}/jobs/{job_id}")
            if job.json()["status"] not in {"queued", "running"}:
                break
            time.sleep(0.01)
        assert job.json()["status"] == "completed"
        assert client.get(f"{base}/attacks").status_code == 200
        matrix = client.get(f"/matters/{matter.matter_id}/evidence-matrix")
        assert matrix.status_code == 200
        assert matrix.json()[0]["claim_id"] == claim.claim_id
        export = client.get(f"/matters/{matter.matter_id}/evidence-matrix.csv")
        assert export.status_code == 200
        assert "Offline matter" in export.text
        locked = client.patch(f"{base}", json={"wording_locked": True})
        assert locked.json()["wording_locked"]
        assert (
            client.patch(
                f"{base}", json={"normalized_proposition": "New wording"}
            ).status_code
            == 400
        )


def test_issue_rerun_is_bounded_to_selected_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class OfflineLLM:
        provider = "offline"
        model = "test"
        retries = 0

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.usage = Usage()

        def generate(
            self,
            prompt: PromptDefinition,
            schema: type[Output],
            context: dict[str, object],
        ) -> Output:
            self.usage.calls += 1
            if prompt.prompt_id == "matter-counter":
                return schema.model_validate({"query": "limitations"})
            evidence = context.get("evidence")
            assert isinstance(evidence, list)
            return schema.model_validate(
                {
                    "judgments": [
                        {
                            "evidence_id": item["evidence_id"],
                            "status": "SUPPORTED"
                            if item["role"] != "counter"
                            else "UNSUPPORTED",
                            "supporting_passage_ids": [item["passage_id"]]
                            if item["role"] != "counter"
                            else [],
                            "explanation": "Offline judgment.",
                        }
                        for item in evidence
                        if isinstance(item, dict)
                    ]
                }
            )

    monkeypatch.setattr("lextrace.api.app.configured_llm", OfflineLLM)
    settings = AppSettings(
        matter_db=tmp_path / "matter.sqlite3",
        private_matter_root=tmp_path / "private",
        llm_model="offline-test",
    )
    store = MatterStore(settings.matter_db, settings.private_matter_root)
    matter = store.create_matter("Issue smoke", court="ca2")
    document = store.add_document(matter.matter_id, "brief.txt", b"The rule applies.")
    span = SourceSpan(document_id=document.document_id, section_id="s", start=0, end=17)
    issue = LegalIssue(
        issue_id="c" * 32,
        matter_id=matter.matter_id,
        label="Retaliation",
        section_ids=["s"],
    )
    claim = LegalClaim(
        claim_id="b" * 32,
        matter_id=matter.matter_id,
        document_id=document.document_id,
        issue_id=issue.issue_id,
        exact_source_text="The rule applies.",
        normalized_proposition="The rule applies.",
        span=span,
    )
    finding = ArgumentFinding(
        claim_id=claim.claim_id,
        issue_id=issue.issue_id,
        proposition=claim.normalized_proposition,
        source=span,
        cited_authorities=[],
        evidence=[],
        counter_authorities=[],
        unresolved_citation_ids=[],
        verification_status="INSUFFICIENT_EVIDENCE",
        research_coverage="NO_RESULTS",
        vulnerability="INSUFFICIENT_EVIDENCE",
        explanation="No verified support.",
    )
    store.save_analysis(
        MatterAnalysis(
            matter_id=matter.matter_id,
            document_id=document.document_id,
            issues=[issue],
            claims=[claim],
            citations=[],
            findings=[finding],
        )
    )
    store.close()
    with TestClient(create_app(retriever=FakeSearch(), settings=settings)) as client:  # type: ignore[arg-type]
        accepted = client.post(
            f"/matters/{matter.matter_id}/issues/{issue.issue_id}/reanalyze"
        )
        assert accepted.status_code == 202
        for _ in range(100):
            job = client.get(
                f"/matters/{matter.matter_id}/jobs/{accepted.json()['job_id']}"
            ).json()
            if job["status"] not in {"queued", "running"}:
                break
            time.sleep(0.01)
        assert job["status"] == "completed"
        assert (
            client.get(f"/matters/{matter.matter_id}/claims/{claim.claim_id}").json()[
                "finding"
            ]["research_coverage"]
            == "SEARCHED"
        )

"""Isolated fictional API smoke; never uses configured operator/data paths.

Run with --live for local Ollama, otherwise deterministic schema responses.
Only counts/state/status are emitted; TEST-ONLY reviews check exact sources first.
"""

import argparse
import json
import secrets
import tempfile
import traceback
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any, TypeVar

from docx import Document
from fastapi.testclient import TestClient
from pydantic import BaseModel, SecretStr

from lextrace.api.app import create_app
from lextrace.config import AppSettings
from lextrace.investigations.contracts import (
    AttributedAssertion,
    EntityMention,
    InvestigationEvent,
)
from lextrace.investigations.evidence_search import EvidenceSearchService
from lextrace.investigations.inconsistency_investigation import (
    InconsistencyInvestigationService,
)
from lextrace.investigations.repository import InvestigationRepository
from lextrace.investigations.review import target_of
from lextrace.investigations.workflow import (
    InvestigationServices,
    InvestigationWorkflow,
)
from lextrace.matter.store import MatterStore
from lextrace.research.contracts import Usage
from lextrace.research.ollama import OllamaLLM
from lextrace.research.prompts import PromptDefinition

SOURCES = {
    "alex.txt": (
        "ALEX MORGAN STATEMENT\nSYNTHETIC TEST DATA - fictional; not legal advice.\n"
        "Alex Morgan states: On March 5, 2025, I arrived around 9:00 AM. "
        "Jordan Lee unloaded three boxes around 9:15 AM.\n"
    ),
    "jordan.txt": (
        "JORDAN LEE STATEMENT\nSYNTHETIC TEST DATA - fictional; not legal advice.\n"
        "Jordan Lee states: On March 5, 2025, I arrived around 11:00 AM. "
        "I did not unload until 11:30 AM; Alex was absent.\n"
    ),
    "log.txt": (
        "WAREHOUSE ENTRANCE LOG\nSYNTHETIC TEST DATA - fictional; not legal advice.\n"
        "March 5, 2025: Delivery truck at 09:05; three boxes received at 09:20; "
        "Jordan Lee badge entry at 11:10.\n"
    ),
}


def smoke(root: Path, provider: Any, *, with_findings: bool = False) -> dict[str, Any]:
    settings = AppSettings(
        operator_id="TEST-ONLY-pilot-reviewer",
        operator_api_secret=SecretStr(secrets.token_urlsafe(36)),
        matter_db=root / "matter.sqlite",
        runtime_db=root / "runtime.sqlite",
        cache_db=root / "cache.sqlite",
        private_matter_root=root / "private",
    )
    assert settings.operator_api_secret is not None
    store = MatterStore(settings.matter_db, settings.private_matter_root)
    repo = InvestigationRepository(settings.matter_db, source_store=store)
    services = InvestigationServices(
        store,
        repo,
        provider,
        InconsistencyInvestigationService(
            store, repo, EvidenceSearchService(store, repo), provider, provider
        ),
    )
    workflow = InvestigationWorkflow.open(services)
    result: dict[str, Any] = {"provider": provider.provider, "model": provider.model}
    try:
        result["last_stage"] = "create_upload_start"
        with TestClient(
            create_app(settings=settings, investigation_workflow=workflow)
        ) as client:
            result["health"] = client.get("/health").status_code
            result["anonymous"] = client.get("/matters").status_code
            client.headers["Authorization"] = (
                "Bearer " + settings.operator_api_secret.get_secret_value()
            )
            matter = client.post(
                "/matters", json={"name": "TEST-ONLY Warehouse Delivery Dispute"}
            ).json()["matter_id"]
            foreign = client.post(
                "/matters", json={"name": "TEST-ONLY Independent Isolation"}
            ).json()["matter_id"]
            base = f"/matters/{matter}/investigations"
            texts: dict[str, str] = {}
            for filename, text in SOURCES.items():
                response = client.post(
                    f"/matters/{matter}/documents",
                    files={"file": (filename, text.encode(), "text/plain")},
                )
                assert (
                    response.status_code == 201
                    and response.json()["ingestion_status"] == "READY"
                )
                texts[response.json()["document_id"]] = text
            result["uploads_ready"] = 3
            eligible = client.get(base + "/eligible-versions").json()
            result["eligible"] = len(eligible)
            response = client.post(
                base,
                json={
                    "request_id": "TEST-ONLY-investigation-start",
                    "approved_versions": [
                        {"document_id": v["document_id"], "version_id": v["version_id"]}
                        for v in eligible
                    ],
                    "budgets": {
                        "model_calls": 10,
                        "extraction_windows": 3,
                        "searches": 1 if with_findings else 0,
                        "investigation": {
                            "max_pairs": 1,
                            "max_searches": 1,
                            "max_model_calls": 2,
                        },
                    },
                },
            )
            result["start_http"] = response.status_code
            if response.status_code != 201:
                return result
            state = response.json()
            thread = state["thread_id"]
            result.update(
                stage=state["stage"],
                checkpoint=state["checkpoint"],
                coverage=state["coverage"],
            )
            result["foreign_read"] = client.get(
                f"/matters/{foreign}/investigations/{thread}"
            ).status_code
            artifacts = workflow.artifacts[thread]
            result["verified"] = [len(v.valid_candidates) for v in artifacts.verified]
            result["extraction_quarantine"] = [
                len(v.extraction_quarantine) for v in artifacts.verified
            ]
            result["verification_quarantine"] = [
                len(v.quarantined_candidates) for v in artifacts.verified
            ]
            result["coverage_issues"] = [
                v.coverage.issue_codes for v in artifacts.verified
            ]
            result["quarantine_reasons"] = [
                [q.reason for q in v.extraction_quarantine] for v in artifacts.verified
            ]
            result["records_by_kind"] = {
                kind: sum(
                    type(c).__name__ == kind
                    for v in artifacts.verified
                    for c in v.valid_candidates
                )
                for kind in [
                    "EntityMention",
                    "AttributedAssertion",
                    "InvestigationEvent",
                    "TemporalReference",
                ]
            }
            result["calls"] = provider.usage.calls
            result["structured_source_records"] = sum(
                c.generator == "investigation-source-accounts@1.0.0"
                for v in artifacts.verified
                for c in v.valid_candidates
            )
            records = [c for v in artifacts.verified for c in v.valid_candidates]
            result["last_stage"] = "attribution_review"
            # Proposed attribution is not yet human-reviewed. In this fictional
            # fixture, only the explicit named-speaker statement is an account.
            # Actor mentions in another person's statement do not make testimony.
            accounts = []
            rejected_attributions: set[str] = set()
            for c in records:
                if not isinstance(c, AttributedAssertion):
                    continue
                source_text = texts[c.document_id]
                expected = next(
                    (
                        name
                        for name, filename in (
                            ("Alex Morgan", "alex.txt"),
                            ("Jordan Lee", "jordan.txt"),
                        )
                        if source_text == SOURCES[filename]
                    ),
                    None,
                )
                if (
                    expected
                    and c.attributed_to == expected
                    and c.attribution_reference is not None
                    and expected in c.attribution_reference.exact_quote
                ):
                    accounts.append(c)
                elif c.attributed_to is not None:
                    rejected_attributions.add(c.assertion_id)
            result["rejected_attribution_proposals"] = len(rejected_attributions)
            events = [c for c in records if isinstance(c, InvestigationEvent)]
            if len({c.document_id for c in accounts}) < 2 or not events:
                result["remaining_stages"] = (
                    "BLOCKED: insufficient attributable accounts/events"
                )
                return result
            result["last_stage"] = "source_invariants"
            for record in records:
                for ref in record.evidence_references:
                    assert ref.matter_id == matter
                    assert (
                        texts[ref.document_id][ref.start : ref.end] == ref.exact_quote
                    )
            for account in accounts:
                assert account.attributed_to is not None
                proof = account.attribution_reference
                assert proof is not None
                assert (
                    texts[proof.document_id][proof.start : proof.end]
                    == proof.exact_quote
                )
                assert account.attributed_to in proof.exact_quote
            result["source_grounded_witness_accounts"] = len(accounts)
            result["grounded_source_checks"] = "PASS"
            url = base + "/" + thread
            assert client.get(url + "/export.docx").status_code == 409
            result["last_stage"] = "prepare_test_review"
            commands = []
            # This optional human fixture association is not a name-based production
            # resolver. It asserts the explicitly fictional participant in both
            # full accounts; speaker dossiers remain separate.
            common_mentions = [
                c
                for c in records
                if isinstance(c, EntityMention)
                and c.mention_text in ("Alex Morgan", "Jordan Lee")
                and all(
                    c.mention_text in a.evidence_references[0].exact_quote
                    for a in accounts
                )
            ]
            selected = common_mentions[0] if with_findings and common_mentions else None
            links = []
            if selected is not None:
                for account in accounts:
                    links.append(
                        dict(
                            account=target_of(account).model_dump(mode="json"),
                            mentions=[target_of(selected).model_dump(mode="json")],
                        )
                    )
            for revision in workflow.services.revisions(artifacts):
                original = revision.original
                target = target_of(original).model_dump(mode="json")
                command = dict(
                    decision_id="TEST-ONLY-accept-" + target["record_id"],
                    timestamp=datetime.now(UTC).isoformat(),
                    target=target,
                    expected_revision=revision.revision,
                    decision="ACCEPT",
                    explanation="TEST-ONLY fictional fixture source verification",
                )
                if (
                    isinstance(original, AttributedAssertion)
                    and original.assertion_id in rejected_attributions
                ):
                    command["decision"] = "REJECT"
                    command["explanation"] = (
                        "TEST-ONLY source does not establish "
                        "the proposed speaker account"
                    )
                if isinstance(original, InvestigationEvent) and any(
                    "March 5, 2025" in ref.exact_quote
                    for ref in original.evidence_references
                ):
                    commands.append(
                        command
                        | dict(
                            decision_id="TEST-ONLY-date-" + target["record_id"],
                            decision="EDIT",
                            date_correction=dict(
                                kind="exact",
                                earliest="2025-03-05",
                                latest="2025-03-05",
                                precision="day",
                            ),
                        )
                    )
                    command["expected_revision"] += 1
                if selected is not None and original == selected:
                    commands.append(
                        command
                        | dict(
                            decision_id="TEST-ONLY-participant-" + target["record_id"],
                            decision="EDIT",
                            resolved_entity_id="TEST-ONLY-fictional-participant",
                        )
                    )
                    command["expected_revision"] += 1
                commands.append(command)

            def approval(view: dict[str, Any]) -> dict[str, Any]:
                return {k: view[k] for k in ("run_id", "token", "digest")} | dict(
                    gate=view["checkpoint"],
                    decision="APPROVE",
                    acknowledge_incomplete_coverage=True,
                )

            request = approval(state) | {
                "evidence_decisions": commands,
                "entity_links": links,
            }
            result["last_stage"] = "evidence_review"
            response = client.post(url + "/decisions", json=request)
            assert response.status_code == 200, response.status_code
            second = response.json()
            result["after_evidence_stage"] = second["stage"]
            result["after_evidence_errors"] = second["errors"]
            intermediate = workflow.artifacts[thread]
            result["candidate_pairs"] = len(intermediate.pairs)
            result["reasoning_diagnostics"] = (
                [d.code for d in intermediate.result.diagnostics]
                if intermediate.result
                else []
            )
            if second["checkpoint"] != "findings":
                result["remaining_stages"] = (
                    "BLOCKED: no findings checkpoint after evidence review"
                )
                return result
            assert client.post(url + "/decisions", json=request).json() == second
            result["evidence_gate_retry"] = "PASS"
            investigation_result = workflow.artifacts[thread].result
            assert investigation_result is not None
            result["reasoning_calls"] = investigation_result.model_calls
            result["reasoning_searches"] = investigation_result.searches
            if with_findings and not investigation_result.findings:
                result["remaining_stages"] = (
                    "BLOCKED: no source-grounded finding produced"
                )
                return result
            for finding in investigation_result.findings:
                for source_account in finding.accounts:
                    ref = source_account.reference
                    assert ref.matter_id == matter
                    assert (
                        texts[ref.document_id][ref.start : ref.end] == ref.exact_quote
                    )
                assert finding.review_state == "PROPOSED"
            result["last_stage"] = "findings_review"
            final = client.post(url + "/decisions", json=approval(second))
            assert final.status_code == 200 and final.json()["stage"] == "APPROVED"
            result["findings_gate"] = "PASS"
            findings_artifacts = workflow.artifacts[thread]
            result["candidate_pairs"] = len(findings_artifacts.pairs)
            result["finding_count"] = (
                len(findings_artifacts.result.findings)
                if findings_artifacts.result
                else 0
            )
            result["finding_verification"] = (
                [f.verification for f in findings_artifacts.result.findings]
                if findings_artifacts.result
                else []
            )
            result["calls_after_findings"] = provider.usage.calls
            result["live_inconsistency"] = (
                "ATTEMPTED_NO_PAIRS"
                if with_findings and not findings_artifacts.pairs
                else "ATTEMPTED"
                if with_findings and selected is not None
                else "BLOCKED: no common reviewed mention"
                if with_findings
                else "NOT_REQUESTED"
            )
            snapshot_url = url + "/snapshots/" + final.json()["snapshot_id"]
            snapshot = client.get(snapshot_url).json()
            chronology = client.get(
                snapshot_url + "/artifacts/" + snapshot["chronology_id"]
            )
            assert chronology.status_code == 200
            reviewed = workflow.artifacts[thread].reviewed_chronology
            assert reviewed is not None
            result["dated_groups"] = len(reviewed.dated_groups)
            assert result["dated_groups"] > 0
            result["last_stage"] = "deposition_create"
            plans = url + "/deposition-plans"
            create = dict(
                snapshot_id=final.json()["snapshot_id"], request_id="TEST-ONLY-create"
            )
            plan_response = client.post(plans, json=create)
            assert plan_response.status_code == 201, plan_response.status_code
            plan = plan_response.json()
            assert plan["basis"]["outcome"] != "BLOCKED"
            assert len(plan["basis"]["witnesses"]) >= 2
            assert client.post(plans, json=create).json() == plan
            plan_url = plans + "/" + plan["basis"]["plan_id"]
            assert client.get(plan_url + "/export.docx").status_code == 409
            result["last_stage"] = "deposition_review"
            questions = plan["current"]["questions"]
            for i, q in enumerate(reversed(questions)):
                q["order"] = i
                q["group"] = "TEST-ONLY preparation"
            questions[0]["wording"] = (
                "Could you clarify the basis of this quoted account?"
            )
            edit = dict(
                decision_id="TEST-ONLY-edit",
                expected_revision=1,
                action="REVISE",
                questions=questions,
            )
            edited = client.post(plan_url + "/review", json={"review": edit})
            assert edited.status_code == 200
            review = edit | dict(
                decision_id="TEST-ONLY-approve",
                expected_revision=2,
                action="APPROVE",
                confirm=True,
                acknowledge_limitations=True,
            )
            approved = client.post(plan_url + "/review", json={"review": review})
            assert approved.status_code == 200
            assert (
                client.post(plan_url + "/review", json={"review": review}).json()
                == approved.json()
            )
            assert (
                client.post(
                    plan_url + "/review",
                    json={"review": edit | {"decision_id": "stale"}},
                ).status_code
                == 409
            )
            result["deposition_questions"] = len(questions)
            result["deposition_review_retry_conflict"] = "PASS"
            result["last_stage"] = "docx_exports"
            before = tuple(repo._db.iterdump())
            assert workflow.runtime is not None
            checkpoints_before = tuple(workflow.runtime.connection.iterdump())
            for path in (url + "/export.docx", plan_url + "/export.docx?revision=3"):
                download = client.get(path)
                assert download.status_code == 200
                doc = Document(BytesIO(download.content))
                content = "\n".join(p.text for p in doc.paragraphs)
                assert "March 5, 2025" in content
                assert "version" in content.lower() and "section" in content.lower()
                for account in accounts:
                    for ref in account.evidence_references:
                        assert ref.exact_quote in content
                        for identity in (
                            ref.document_id,
                            ref.version_id,
                            ref.section_id,
                        ):
                            assert identity in content
            assert tuple(repo._db.iterdump()) == before
            assert tuple(workflow.runtime.connection.iterdump()) == checkpoints_before
            result["docx_readback"] = "PASS"
            assert (
                client.get(
                    f"/matters/{foreign}/investigations/{thread}/export.docx"
                ).status_code
                == 404
            )
        result["last_stage"] = "restart"
        workflow.close()
        workflow = InvestigationWorkflow.open(services)
        with TestClient(
            create_app(settings=settings, investigation_workflow=workflow)
        ) as recovered:
            recovered.headers["Authorization"] = (
                "Bearer " + settings.operator_api_secret.get_secret_value()
            )
            assert recovered.get(url).json()["snapshot_id"] == snapshot["snapshot_id"]
            assert recovered.get(plan_url).json()["current"]["status"] == "APPROVED"
            assert (
                recovered.get(plan_url + "/export.docx?revision=3").status_code == 200
            )
        result["restart"] = "PASS"
        result["remaining_stages"] = (
            "PASS (TEST-ONLY approvals; see live_inconsistency result)"
        )
        return result
    except Exception as error:
        result["remaining_stages"] = "FAIL: smoke invariant"
        result["error_type"] = type(error).__name__
        result["failed_invariant_line"] = traceback.extract_tb(error.__traceback__)[
            -1
        ].lineno
        result["calls_at_failure"] = provider.usage.calls
        return result
    finally:
        workflow.close()
        repo.close()
        store.close()


Output = TypeVar("Output", bound=BaseModel)


class FixtureLLM:
    """Explicit offline fixture proposals; not evidence of model quality."""

    provider = "deterministic-fixture"
    model = "warehouse-v1"
    retries = 0

    def __init__(self) -> None:
        self.usage = Usage()

    def generate(
        self, prompt: PromptDefinition, schema: type[Output], context: dict[str, object]
    ) -> Output:
        self.usage.calls += 1
        text = str(context["text"])
        quote = text.split("\n")[2]
        speaker = next(
            (
                name
                for name in ("Alex Morgan", "Jordan Lee")
                if quote.startswith(name + " states:")
            ),
            None,
        )
        common = dict(quote=quote)
        candidates = [common | {"kind": "event"}]
        if speaker:
            candidates.append(
                common
                | dict(
                    kind="assertion",
                    speaker_quote=speaker,
                    predicate_quote="arrived",
                    subject_quote="I",
                    object_quote="around 9:00 AM"
                    if speaker == "Alex Morgan"
                    else "around 11:00 AM",
                )
            )
        candidates.append(dict(kind="entity", quote="Jordan Lee", entity_type="person"))
        return schema.model_validate({"candidates": candidates})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--model", default="qwen3:8b")
    parser.add_argument("--with-findings", action="store_true")
    args = parser.parse_args()
    provider = (
        OllamaLLM(
            args.model,
            timeout=90,
            max_retries=0,
            max_completion_tokens=8192,
            context_tokens=16384,
        )
        if args.live
        else FixtureLLM()
    )
    try:
        with tempfile.TemporaryDirectory(prefix="lextrace-pilot-") as directory:
            result = smoke(Path(directory), provider, with_findings=args.with_findings)
            print(json.dumps(result, indent=2))
            if not str(result.get("remaining_stages", "")).startswith("PASS"):
                raise SystemExit(2)
    except Exception as error:
        # CLI status only. API and test failures must never dump private paths or
        # provider exception bodies; pytest still exercises smoke() directly.
        print(json.dumps({"result": "FAIL", "error_type": type(error).__name__}))
        raise SystemExit(1) from None
    finally:
        if isinstance(provider, OllamaLLM):
            provider.close()


if __name__ == "__main__":
    main()

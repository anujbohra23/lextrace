# ruff: noqa: F811 -- shared synthetic fixtures
"""Export trust boundaries, partial coverage, mutation and restart regressions."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from lextrace.deposition.contracts import PlanView
from lextrace.deposition.service import DepositionService
from lextrace.exports.service import ExportService
from lextrace.investigations.workflow import ApprovedInvestigationSnapshot
from tests.api.conftest import (
    operator_environment as operator_environment,  # noqa: F401
)
from tests.api.test_exports import paths
from tests.api.test_investigations import client as client  # noqa: F401
from tests.deposition_fixtures import deposition as deposition  # noqa: F401
from tests.exports_fixtures import approved_plan as approved_plan  # noqa: F401
from tests.exports_fixtures import exporter as exporter  # noqa: F401
from tests.unit.test_docx_exports import paragraphs
from tests.unit.test_investigation_durable import Harness
from tests.unit.test_investigation_durable import durable as durable  # noqa: F401
from tests.unit.test_investigation_workflow import setup as memory_setup  # noqa: F401


@pytest.mark.parametrize(
    "damage",
    [
        "source_text",
        "original_bytes",
        "version",
        "review",
        "missing_journal",
        "corrupt_journal",
        "deleted",
    ],
)
def test_broken_dependencies_never_export(
    client: TestClient,
    deposition: DepositionService,
    approved_plan: PlanView,
    durable: Harness,
    damage: str,
) -> None:
    b = approved_plan.basis
    source = b.evidence[0].reference
    repo = durable.runtime.repo
    if damage in ("source_text", "original_bytes"):
        name = "extracted.txt" if damage == "source_text" else "source.txt"
        (durable.root / "private" / b.matter_id / source.document_id / name).write_text(
            "modified source"
        )
    elif damage == "version":
        old = repo.active_document_version(b.matter_id, source.document_id)
        assert old is not None
        repo.create_document_version(
            old.model_copy(update={"version_id": "later-version"})
        )
    elif damage == "review":
        from tests.unit.test_investigation_review import command

        old_review = b.witnesses[0].accounts[0]
        repo.review(
            command(
                old_review.original,
                expected_revision=old_review.revision,
                decision="REJECT",
                decision_id="after-approval",
            )
        )
    elif damage == "deleted":
        durable.runtime.store.delete_document(b.matter_id, source.document_id)
    else:
        with repo._db as db:
            if damage == "missing_journal":
                db.execute(
                    "DELETE FROM investigation_artifacts "
                    "WHERE thread_id=? AND digest=?",
                    (b.thread_id, b.artifact_digest),
                )
            else:
                db.execute(
                    "UPDATE investigation_artifacts SET payload=x'00' "
                    "WHERE thread_id=? AND digest=?",
                    (b.thread_id, b.artifact_digest),
                )
    for url in paths(deposition, approved_plan):
        response = client.get(url)
        assert response.status_code == (404 if damage == "deleted" else 409), (
            response.text
        )
        assert response.headers["content-type"].startswith("application/json")
        assert not response.content.startswith(b"PK")


def test_read_only_export_survives_restart_without_changing_journals(
    deposition: DepositionService,
    approved_plan: PlanView,
    durable: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    b = approved_plan.basis
    restarted = DepositionService(durable.restart())
    exporter = ExportService(restarted)
    matter = durable.runtime.store.get_matter(b.matter_id)
    assert matter is not None
    tables = (
        "investigation_artifacts",
        "investigation_snapshots",
        "investigation_intents",
        "deposition_plans",
        "deposition_revisions",
        "deposition_decisions",
    )
    before = {
        t: restarted.runtime.read(f"SELECT * FROM {t} ORDER BY rowid") for t in tables
    }

    def forbidden(thread: str) -> None:
        raise AssertionError("Export invoked workflow recovery")

    monkeypatch.setattr(restarted.workflow, "reopen", forbidden)
    signed_read = restarted.workflow.approved_snapshot

    def replace_working(thread: str) -> ApprovedInvestigationSnapshot:
        signed = signed_read(thread)
        restarted.workflow.artifacts[thread].verified = ()
        restarted.workflow.artifacts[thread].result = None
        return signed

    monkeypatch.setattr(restarted.workflow, "approved_snapshot", replace_working)
    assert paragraphs(exporter.investigation(matter, b.thread_id).content)
    assert paragraphs(
        exporter.deposition_plan(matter, b.thread_id, b.plan_id, 2).content
    )
    after = {
        t: restarted.runtime.read(f"SELECT * FROM {t} ORDER BY rowid") for t in tables
    }
    assert before == after


@pytest.mark.parametrize("mode", ["partial", "empty", "quarantine"])
def test_partial_and_empty_reviewed_sections_are_explicit(
    client: TestClient, durable: Harness, mode: str
) -> None:
    from lextrace.deposition.contracts import ReviewPlan
    from lextrace.investigations.contracts import EntityMention
    from tests.unit.test_investigation_workflow import approval, linked

    if mode == "empty":
        durable.services.verified = tuple(
            replace(
                v,
                valid_candidates=tuple(
                    c for c in v.valid_candidates if isinstance(c, EntityMention)
                ),
            )
            for v in durable.services.verified
        )
    elif mode == "quarantine":
        from lextrace.investigations.extraction_verification import (
            VerificationQuarantine,
        )

        candidate = durable.services.verified[0].valid_candidates[0]
        bad = candidate.model_copy(
            update={
                "evidence_references": (
                    candidate.evidence_references[0].model_copy(
                        update={"exact_quote": "FORGED UNREVIEWED QUOTE"}
                    ),
                )
            }
        )
        durable.services.verified = tuple(
            replace(
                v,
                quarantined_candidates=(
                    VerificationQuarantine(0, bad, ("QUOTE_MISMATCH",)),
                ),
            )
            for v in durable.services.verified
        )
    else:
        durable.services.verified = tuple(
            replace(v, coverage=replace(v.coverage, status="PARTIAL"))
            for v in durable.services.verified
        )
    first = durable.start()
    gate = durable.workflow.resume(
        linked(durable.workflow, first).model_copy(
            update={"acknowledge_incomplete_coverage": True}
        )
    )
    final = durable.workflow.resume(
        approval(gate, acknowledge_incomplete_coverage=True)
    )
    assert final.stage == "APPROVED"
    base = f"/matters/{first.matter_id}/investigations/{first.thread_id}"
    response = client.get(base + "/export.docx")
    assert response.status_code == 200, (
        response.text[:100] if not response.content.startswith(b"PK") else ""
    )
    text = "\n".join(paragraphs(response.content))
    if mode == "empty":
        assert "No reviewed attributed accounts were recorded" in text
        assert "No reviewed investigative findings were recorded" in text
        assert "No reviewed chronology events were recorded" in text
    else:
        if mode == "partial":
            assert "PARTIAL OR INCOMPLETE COVERAGE" in text
        else:
            assert "Quarantined candidates excluded from reviewed content: 1" in text
            assert "FORGED UNREVIEWED QUOTE" not in text
        service = DepositionService(durable.workflow)
        snapshot = service.workflow.approved_snapshot(first.thread_id)
        view = service.create(
            first.matter_id,
            first.thread_id,
            snapshot.snapshot_id,
            "partial-export",
            "human",
        )
        approved = service.review(
            first.matter_id,
            first.thread_id,
            view.basis.plan_id,
            ReviewPlan(
                decision_id="approve-partial",
                expected_revision=1,
                action="APPROVE",
                questions=view.current.questions,
                confirm=True,
                acknowledge_limitations=True,
            ),
            "human",
        )
        response = client.get(
            base + f"/deposition-plans/{approved.basis.plan_id}/export.docx"
        )
        assert response.status_code == 200
        exported = "\n".join(paragraphs(response.content))
        assert (
            "PARTIAL OR INCOMPLETE COVERAGE"
            if mode == "partial"
            else "CANDIDATES_QUARANTINED"
        ) in exported
        assert "FORGED UNREVIEWED QUOTE" not in exported

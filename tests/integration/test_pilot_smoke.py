"""Full offline API chain; genuinely live acceptance is a separate script run."""

from pathlib import Path

from scripts.pilot_smoke import FixtureLLM, smoke


def test_isolated_warehouse_api_through_restart_and_both_word_exports(
    tmp_path: Path,
) -> None:
    result = smoke(tmp_path, FixtureLLM())
    assert result["records_by_kind"]["InvestigationEvent"] == 3
    assert result["records_by_kind"]["AttributedAssertion"] == 2
    assert result["dated_groups"] == 1
    assert result["anonymous"] == 401
    assert result["foreign_read"] == 404
    for stage in [
        "grounded_source_checks",
        "evidence_gate_retry",
        "findings_gate",
        "deposition_review_retry_conflict",
        "docx_readback",
        "restart",
    ]:
        assert result[stage] == "PASS"


def test_start_retry_reuses_durable_request_identity(
    tmp_path: Path, monkeypatch: object
) -> None:
    """The smoke sends a stable request ID across an uncertain response."""
    import pytest
    from fastapi.testclient import TestClient

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    original = TestClient.post
    starts: list[str] = []

    def duplicate(self: TestClient, url: str, **kwargs: object) -> object:
        if url.endswith("/investigations"):
            first = original(self, url, **kwargs)  # type: ignore[arg-type]
            second = original(self, url, **kwargs)  # type: ignore[arg-type]
            assert first.status_code == second.status_code == 201
            starts.extend([first.json()["thread_id"], second.json()["thread_id"]])
            return first
        return original(self, url, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(TestClient, "post", duplicate)
    result = smoke(tmp_path, FixtureLLM())
    assert len(set(starts)) == 1
    assert result["restart"] == "PASS"


def test_real_candidate_selection_reaches_reasoner_and_fails_closed(
    tmp_path: Path,
) -> None:
    # FixtureLLM intentionally implements extraction only. The real M6 service
    # must reject its missing reasoning schema, never publish invented findings.
    result = smoke(tmp_path, FixtureLLM(), with_findings=True)
    assert result["candidate_pairs"] == 1
    assert result["after_evidence_stage"] == "BLOCKED"
    assert "MODEL_FAILED" in result["reasoning_diagnostics"]
    assert "findings_gate" not in result
    assert result["remaining_stages"].startswith("BLOCKED")


def test_proposed_actor_attribution_is_rejected_not_assumed_reviewed(
    tmp_path: Path,
) -> None:
    from typing import TypeVar

    from pydantic import BaseModel

    from lextrace.research.prompts import PromptDefinition

    Output = TypeVar("Output", bound=BaseModel)

    class ExtraActor(FixtureLLM):
        def generate(
            self,
            prompt: PromptDefinition,
            schema: type[Output],
            context: dict[str, object],
        ) -> Output:
            output = super().generate(prompt, schema, context)
            data = output.model_dump()
            if str(context["text"]).startswith("ALEX"):
                data["candidates"].append(
                    dict(
                        kind="assertion",
                        quote="Jordan Lee unloaded three boxes around 9:15 AM.",
                        speaker_quote="Jordan Lee",
                        subject_quote="Jordan Lee",
                        predicate_quote="unloaded",
                        object_quote="three boxes",
                    )
                )
            return schema.model_validate(data)

    result = smoke(tmp_path, ExtraActor())
    # Server structure identifies Alex as the source of the third-person report;
    # Jordan is an actor, not an independently inferred speaker.
    assert result["rejected_attribution_proposals"] == 0
    assert result["restart"] == "PASS", result


def test_missing_supported_speaker_blocks_before_any_approval(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    import sqlite3
    from typing import TypeVar

    from pydantic import BaseModel

    from lextrace.research.prompts import PromptDefinition

    Output = TypeVar("Output", bound=BaseModel)

    class WrongSpeaker(FixtureLLM):
        def generate(
            self,
            prompt: PromptDefinition,
            schema: type[Output],
            context: dict[str, object],
        ) -> Output:
            output = super().generate(prompt, schema, context)
            data = output.model_dump()
            if str(context["text"]).startswith("JORDAN"):
                for candidate in data["candidates"]:
                    if candidate["kind"] == "assertion":
                        # The literal name occurs in the source, but the source
                        # attributes this statement to Jordan, not the absent Alex.
                        candidate["speaker_quote"] = "Alex"
            return schema.model_validate(data)

    import pytest

    from scripts.pilot_smoke import SOURCES

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    monkeypatch.setitem(
        SOURCES,
        "jordan.txt",
        SOURCES["jordan.txt"]
        .replace("JORDAN LEE STATEMENT", "DELIVERY NOTES")
        .replace("Jordan Lee states: ", ""),
    )
    result = smoke(tmp_path, WrongSpeaker(), with_findings=True)
    assert result["rejected_attribution_proposals"] == 0
    assert (
        result["remaining_stages"]
        == "BLOCKED: insufficient attributable accounts/events"
    )
    assert "after_evidence_stage" not in result
    with sqlite3.connect(tmp_path / "matter.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM investigation_intents").fetchone() == (
            0,
        )
        assert db.execute(
            "SELECT COUNT(*) FROM investigation_snapshots"
        ).fetchone() == (0,)

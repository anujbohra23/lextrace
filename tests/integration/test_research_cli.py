"""Research CLI rendering and safe configuration failures."""

import json
from argparse import ArgumentParser
from pathlib import Path

import pytest

from lextrace.cli import main
from lextrace.research.commands import add_command, run_command
from lextrace.research.contracts import ResearchRequest, ResearchResponse


class ClosedRetriever:
    def close(self) -> None:
        pass


class StubWorkflow:
    retriever = ClosedRetriever()

    def __init__(self, response: ResearchResponse) -> None:
        self.response = response

    def run(self, request: ResearchRequest) -> ResearchResponse:
        assert request.max_cases == 3
        return self.response


def test_research_json_output(
    empty_research_response: ResearchResponse,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = ArgumentParser()
    commands = parser.add_subparsers(dest="command")
    add_command(commands)
    args = parser.parse_args(
        [
            "research",
            "Question",
            "--index",
            "index",
            "--max-cases",
            "3",
            "--json",
        ]
    )
    assert run_command(args, parser, workflow=StubWorkflow(empty_research_response))
    result = capsys.readouterr()
    assert not result.err
    assert json.loads(result.out)["run_id"] == "run-1"


def test_missing_llm_configuration_is_safe(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LEXTRACE_LLM_MODEL", raising=False)
    with pytest.raises(SystemExit) as captured:
        main(["research", "Question", "--index", "missing"])
    assert captured.value.code == 1
    result = capsys.readouterr()
    assert not result.out
    assert result.err == "Error: LLM model is not configured.\n"


def test_evaluate_system_command(
    tmp_path: Path,
    empty_research_response: ResearchResponse,
    capsys: pytest.CaptureFixture[str],
) -> None:
    golden = tmp_path / "golden.json"
    golden.write_text(json.dumps([empty_research_response.model_dump(mode="json")]))
    main(["evaluate-system", "--golden", str(golden)])
    metrics = json.loads(capsys.readouterr().out)
    assert metrics["runs"] == 1
    assert metrics["completion_rate"] == 1

"""CLI construction and rendering for grounded legal research."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Protocol

from lextrace.config import AppSettings, ConfigurationError
from lextrace.graph.contracts import GraphError
from lextrace.research.contracts import ResearchError, ResearchRequest, ResearchResponse
from lextrace.research.evaluation import evaluate_runs
from lextrace.research.llm import configured_llm
from lextrace.research.workflow import ResearchWorkflow
from lextrace.retrieval.contracts import RetrievalError
from lextrace.retrieval.engine import LexTraceRetriever


class ResearchRunner(Protocol):
    def run(self, request: ResearchRequest) -> ResearchResponse: ...


def add_command(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser("research", help="Run grounded legal research")
    parser.add_argument("question")
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--graph", type=Path)
    parser.add_argument("--jurisdiction")
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--max-cases", type=int, default=8, choices=range(1, 21))
    parser.add_argument(
        "--provider", choices=["openai-compatible", "ollama"], default=None
    )
    parser.add_argument("--model", help="Provider model ID; or set LEXTRACE_LLM_MODEL")
    parser.add_argument("--json", action="store_true")
    evaluation = commands.add_parser(
        "evaluate-system", help="Evaluate saved research responses offline"
    )
    evaluation.add_argument("--golden", type=Path, required=True)


def build_workflow(
    index_path: Path,
    graph_path: Path | None,
    model_name: str | None,
    *,
    settings: AppSettings | None = None,
    provider_name: str | None = None,
) -> ResearchWorkflow:
    configured = settings or AppSettings.from_environment()
    model = model_name or configured.llm_model
    if not model:
        raise ResearchError("LLM model is not configured.")
    retriever = LexTraceRetriever.from_index(index_path, graph_path=graph_path)
    llm = configured_llm(
        model,
        provider=provider_name or configured.llm_provider,
        openai_base_url=configured.llm_base_url,
        ollama_base_url=configured.ollama_base_url,
        timeout=configured.llm_timeout,
    )
    return ResearchWorkflow(retriever, llm)


def render(response: ResearchResponse, *, as_json: bool) -> str:
    if as_json:
        return response.model_dump_json(indent=2)
    memo = response.final_memo
    lines = [f"Run: {response.run_id}"]
    lines.append(
        "Issues: " + ", ".join(issue.label for issue in response.identified_issues)
    )
    if memo is None:
        lines.append("No grounded memo could be produced.")
    else:
        lines.extend(
            [
                memo.title,
                memo.disclaimer,
                "Question: " + memo.question_presented,
                "Analysis:",
                *[f"- {item}" for item in memo.analysis],
                "Supporting arguments:",
                *[f"- {item}" for item in memo.supporting_arguments],
                "Counterarguments:",
                *[f"- {item}" for item in memo.counterarguments],
                "Conclusion: " + memo.research_conclusion,
            ]
        )
    summary = response.grounding_summary
    lines.append(
        f"Grounding: {summary.supported}/{summary.total_substantive_claims} "
        f"supported ({summary.confidence})"
    )
    if response.warnings:
        lines.extend(["Warnings:", *[f"- {warning}" for warning in response.warnings]])
    lines.append("Sources:")
    for item in response.relevant_cases:
        result = item.result
        citation = ", ".join(result.reporter_citations or [])
        label = f"{result.case_name} — {result.court}"
        if result.date_filed:
            label += f", {result.date_filed.isoformat()}"
        if citation:
            label += f", {citation}"
        lines.append(
            f"- {label}: {result.source_url} [{result.relevant_passage.passage_id}]"
        )
    return "\n".join(lines)


def run_command(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    *,
    workflow: ResearchRunner | None = None,
) -> bool:
    if args.command == "evaluate-system":
        try:
            raw = json.loads(args.golden.read_text())
            if not isinstance(raw, list):
                raise ValueError
            responses = [ResearchResponse.model_validate(item) for item in raw]
        except (OSError, ValueError):
            parser.exit(1, "Error: Golden research responses are invalid.\n")
        print(evaluate_runs(responses).model_dump_json(indent=2))
        return True
    if args.command != "research":
        return False
    current = workflow
    try:
        current = current or build_workflow(
            args.index, args.graph, args.model, provider_name=args.provider
        )
        response = current.run(
            ResearchRequest(
                question=args.question,
                jurisdiction=args.jurisdiction,
                as_of_date=args.as_of,
                max_cases=args.max_cases,
            )
        )
    except (ResearchError, RetrievalError, GraphError, ConfigurationError) as error:
        parser.exit(1, f"Error: {error}\n")
    finally:
        if workflow is None and isinstance(current, ResearchWorkflow):
            current.retriever.close()
    print(render(response, as_json=args.json))
    return True

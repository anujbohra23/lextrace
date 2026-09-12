"""CLI construction and rendering for grounded legal research."""

from __future__ import annotations

import argparse
import os
from datetime import date
from pathlib import Path
from typing import Protocol

from lextrace.graph.contracts import GraphError
from lextrace.research.contracts import ResearchError, ResearchRequest, ResearchResponse
from lextrace.research.llm import OpenAICompatibleLLM
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
        "--provider", choices=["openai-compatible"], default="openai-compatible"
    )
    parser.add_argument("--model", help="Provider model ID; or set LEXTRACE_LLM_MODEL")
    parser.add_argument("--json", action="store_true")


def build_workflow(
    index_path: Path, graph_path: Path | None, model_name: str | None
) -> ResearchWorkflow:
    model = model_name or os.environ.get("LEXTRACE_LLM_MODEL")
    credential = os.environ.get("OPENAI_API_KEY")
    if not model:
        raise ResearchError("LLM model is not configured.")
    if not credential:
        raise ResearchError("LLM API credential is missing.")
    retriever = LexTraceRetriever.from_index(index_path, graph_path=graph_path)
    llm = OpenAICompatibleLLM(
        model,
        api_key=credential,
        base_url=os.environ.get("OPENAI_BASE_URL"),
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
    if args.command != "research":
        return False
    current = workflow
    try:
        current = current or build_workflow(args.index, args.graph, args.model)
        response = current.run(
            ResearchRequest(
                question=args.question,
                jurisdiction=args.jurisdiction,
                as_of_date=args.as_of,
                max_cases=args.max_cases,
            )
        )
    except (ResearchError, RetrievalError, GraphError) as error:
        parser.exit(1, f"Error: {error}\n")
    finally:
        if workflow is None and isinstance(current, ResearchWorkflow):
            current.retriever.close()
    print(render(response, as_json=args.json))
    return True

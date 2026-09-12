"""CLI commands for citation graph building, inspection, and traversal."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from lextrace.graph.build import build_graph
from lextrace.graph.contracts import Direction
from lextrace.graph.store import CitationGraph


def add_commands(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    build = commands.add_parser(
        "build-citation-graph", help="Build or reuse a local citation graph"
    )
    build.add_argument("--corpus", type=Path, required=True)
    build.add_argument("--citation-source", type=Path, required=True)
    build.add_argument("--output", type=Path, default=Path("artifacts/graphs/default"))
    info = commands.add_parser("graph-info", help="Validate and describe a graph")
    info.add_argument("graph", type=Path)
    citations = commands.add_parser("citations", help="List a case's citation links")
    citations.add_argument("case_id")
    citations.add_argument(
        "--graph", type=Path, default=Path("artifacts/graphs/default")
    )
    citations.add_argument(
        "--direction", choices=["outgoing", "incoming", "both"], default="outgoing"
    )
    citations.add_argument("--limit", type=int, default=100)
    citations.add_argument("--as-of-date")
    citations.add_argument("--json", action="store_true")


def run_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> bool:
    if args.command == "build-citation-graph":
        print(
            build_graph(args.corpus, args.citation_source, args.output).model_dump_json(
                indent=2
            )
        )
        return True
    if args.command == "graph-info":
        graph = CitationGraph(args.graph)
        try:
            print(graph.metadata.model_dump_json(indent=2))
        finally:
            graph.close()
        return True
    if args.command != "citations":
        return False
    try:
        direction: Direction = TypeAdapter(Direction).validate_python(args.direction)
        as_of: date | None = TypeAdapter(date | None).validate_python(args.as_of_date)
    except ValidationError:
        parser.error("Invalid citation direction or as-of date.")
    graph = CitationGraph(args.graph)
    try:
        if graph.get_node(args.case_id) is None:
            parser.error("Unknown citation graph case ID.")
        rows = graph.neighbors(
            args.case_id,
            direction=direction,
            limit=args.limit,
            as_of_date=as_of,
        )
        if args.json:
            print("[" + ",\n".join(row.model_dump_json(indent=2) for row in rows) + "]")
        else:
            for row in rows:
                citation = "; ".join(row.node.reporter_citations or [])
                print(
                    f"{row.direction}: {row.node.case_name or row.node.case_id} "
                    f"({row.node.court or 'court unknown'}, "
                    f"{row.node.date_filed or 'date unknown'})"
                )
                if citation:
                    print("   " + citation)
                if row.node.source_url:
                    print("   " + row.node.source_url)
        return True
    finally:
        graph.close()

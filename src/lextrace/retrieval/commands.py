"""CLI adapters for local indexes and search, without ingestion credentials."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pydantic import ValidationError

from lextrace.retrieval.contracts import SearchFilters, SearchRequest
from lextrace.retrieval.engine import LexTraceRetriever
from lextrace.retrieval.index import LocalIndex, build_index
from lextrace.retrieval.settings import read_config


def add_commands(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    build = commands.add_parser(
        "build-index", help="Build or reuse a local retrieval index"
    )
    build.add_argument("--corpus", type=Path, required=True)
    build.add_argument("--output", type=Path, default=Path("artifacts/indexes/default"))
    build.add_argument("--config", type=Path)
    build.add_argument(
        "--lexical-only", action="store_true", help="Build without embedding models"
    )
    info = commands.add_parser("index-info", help="Validate and describe a local index")
    info.add_argument("index", type=Path)
    search = commands.add_parser("search", help="Search a local normalized case corpus")
    search.add_argument("query")
    search.add_argument("--index", type=Path, default=Path("artifacts/indexes/default"))
    search.add_argument("--corpus", type=Path, help="Optional source hash cross-check")
    search.add_argument(
        "--config", type=Path, help="Runtime settings; representation must match index"
    )
    search.add_argument("--mode", choices=["bm25", "dense", "hybrid", "reranked"])
    search.add_argument("--top-k", type=int, default=10)
    search.add_argument("--court", action="append", default=[])
    search.add_argument("--filed-after")
    search.add_argument("--filed-before")
    search.add_argument("--json", action="store_true")
    evaluate = commands.add_parser(
        "evaluate-engine", help="Evaluate local modes on a valid benchmark"
    )
    evaluate.add_argument("bundle", type=Path)
    evaluate.add_argument("--index", type=Path, required=True)
    evaluate.add_argument("--output", type=Path, required=True)
    evaluate.add_argument(
        "--modes",
        nargs="+",
        choices=["bm25", "dense", "hybrid", "reranked"],
        default=["bm25", "dense", "hybrid", "reranked"],
    )


def run_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> bool:
    if args.command == "build-index":
        metadata = build_index(
            args.corpus,
            args.output,
            read_config(args.config),
            dense=not args.lexical_only,
        )
        print(metadata.model_dump_json(indent=2))
        return True
    if args.command == "index-info":
        print(LocalIndex(args.index).metadata.model_dump_json(indent=2))
        return True
    if args.command == "evaluate-engine":
        from lextrace.evaluation.engine_run import evaluate_engine

        print(
            json.dumps(
                evaluate_engine(args.bundle, args.index, args.output, modes=args.modes),
                indent=2,
            )
        )
        return True
    if args.command != "search":
        return False
    try:
        filters = SearchFilters.model_validate(
            {
                "courts": args.court,
                "filed_after": args.filed_after,
                "filed_before": args.filed_before,
            }
        )
        # Validate input before loading index or model resources.
        request = SearchRequest(
            query=args.query,
            top_k=args.top_k,
            mode=args.mode or "reranked",
            filters=filters,
        )
    except ValidationError:
        parser.error("Invalid search query, top-k, or metadata filters.")
    engine = LexTraceRetriever.from_index(
        args.index,
        corpus_path=args.corpus,
        config=read_config(args.config) if args.config else None,
    )
    if args.mode is None:
        request.mode = engine.config.default_mode
    try:
        response = engine.search_response(request)
    finally:
        engine.close()
    if args.json:
        print(response.model_dump_json(indent=2))
    else:
        for result in response.results:
            print(
                f"{result.rank}. {result.case_name} "
                f"({result.court}, {result.date_filed or 'date unknown'})"
            )
            print(
                "   "
                + (
                    "; ".join(result.reporter_citations or [])
                    or "Reporter citation unavailable"
                )
            )
            print(f"   Score: {result.final_score:.6f} [{result.retrieval_method}]")
            print("   " + " ".join(result.relevant_passage.text.split()))
            print("   " + str(result.source_url))
            print(
                "   Diagnostics: "
                + result.diagnostics.model_dump_json(exclude_none=True)
            )
        if not response.results:
            print("No cases matched the filters.")
        print(
            "Stage seconds: " + json.dumps(response.trace.stage_seconds, sort_keys=True)
        )
    return True

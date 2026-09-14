"""LexTrace command-line entry point."""

import argparse
import json
import math
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import httpx
from pydantic import ValidationError

from lextrace.config import ConfigurationError, courtlistener_token
from lextrace.corpus import CorpusError, CorpusQuery
from lextrace.evaluation.benchmark import BenchmarkError
from lextrace.evaluation.benchmark_build import build_benchmark, validate_benchmark
from lextrace.evaluation.corpus_quality import inspect_corpus
from lextrace.evaluation.retrieval_run import run_bm25
from lextrace.graph.commands import add_commands as add_graph_commands
from lextrace.graph.commands import run_command as run_graph_command
from lextrace.graph.contracts import GraphError
from lextrace.ingestion.benchmark_job import acquire_benchmark
from lextrace.ingestion.corpus import ingest_corpus
from lextrace.ingestion.courtlistener import IngestionError, fetch_case
from lextrace.ingestion.normalize import normalize_case
from lextrace.matter.monitoring_commands import (
    add_command as add_monitoring_command,
)
from lextrace.matter.monitoring_commands import (
    run_command as run_monitoring_command,
)
from lextrace.research.commands import add_command as add_research_command
from lextrace.research.commands import run_command as run_research_command
from lextrace.research.contracts import ResearchError
from lextrace.retrieval.commands import add_commands, run_command
from lextrace.retrieval.contracts import RetrievalError


def _cluster_id(value: str) -> int:
    if not value.isascii() or not value.isdecimal() or int(value) <= 0:
        raise argparse.ArgumentTypeError("cluster_id must be a positive integer")
    return int(value)


def _interval(value: str) -> float:
    try:
        interval = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "request interval must be a positive number"
        ) from None
    if not math.isfinite(interval) or interval <= 0:
        raise argparse.ArgumentTypeError(
            "request interval must be a finite positive number"
        )
    return interval


def main(
    argv: Sequence[str] | None = None, *, transport: httpx.BaseTransport | None = None
) -> None:
    """Ingest one decision cluster and print its complete normalized JSON."""
    parser = argparse.ArgumentParser(description="LexTrace research tools")
    commands = parser.add_subparsers(dest="command")
    ingest = commands.add_parser("ingest-case", help="Ingest a CourtListener cluster")
    ingest.add_argument("cluster_id", type=_cluster_id)
    corpus = commands.add_parser("ingest-corpus", help="Build a bounded local corpus")
    corpus.add_argument("--court", required=True)
    corpus.add_argument("--filed-after", type=date.fromisoformat)
    corpus.add_argument("--filed-before", type=date.fromisoformat)
    corpus.add_argument("--max-cases", type=_cluster_id, required=True)
    corpus.add_argument("--output", type=Path, required=True)
    corpus.add_argument(
        "--request-interval",
        type=_interval,
        required=True,
        help="Minimum seconds between requests; choose for your account limits",
    )
    corpus.add_argument("--resume", action="store_true")
    inspect = commands.add_parser(
        "inspect-corpus", help="Inspect normalized corpus quality offline"
    )
    inspect.add_argument("path", type=Path)
    benchmark = commands.add_parser(
        "build-benchmark", help="Build a reviewed citation-recovery bundle offline"
    )
    benchmark.add_argument("--corpus", type=Path, required=True)
    benchmark.add_argument("--inputs", type=Path, required=True)
    benchmark.add_argument(
        "--config", type=Path, default=Path("experiments/configs/benchmark_v1.json")
    )
    benchmark.add_argument("--output", type=Path, required=True)
    validate = commands.add_parser(
        "validate-benchmark", help="Verify a frozen citation-recovery bundle offline"
    )
    validate.add_argument("bundle", type=Path)
    acquisition = commands.add_parser(
        "acquire-benchmark", help="Acquire resumable provisional V1 source inputs"
    )
    acquisition.add_argument("--output", type=Path, required=True)
    acquisition.add_argument("--request-interval", type=_interval, default=15)
    acquisition.add_argument("--timeout", type=_interval, default=90)
    acquisition.add_argument("--max-requests", type=_cluster_id, default=40)
    baseline = commands.add_parser(
        "run-bm25", help="Evaluate BM25 on a validated local benchmark"
    )
    baseline.add_argument("bundle", type=Path)
    baseline.add_argument("--output", type=Path, required=True)
    add_commands(commands)
    add_graph_commands(commands)
    add_research_command(commands)
    add_monitoring_command(commands)
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return
    try:
        if (
            run_command(args, parser)
            or run_graph_command(args, parser)
            or run_research_command(args, parser)
            or run_monitoring_command(args, parser)
        ):
            return
        if args.command == "acquire-benchmark":
            result = acquire_benchmark(
                args.output,
                courtlistener_token(),
                interval=args.request_interval,
                timeout=args.timeout,
                max_requests=args.max_requests,
                transport=transport,
            )
            print(json.dumps(result, sort_keys=True, indent=2))
            if result["status"] != "ready_for_build":
                parser.exit(1, "Acquisition incomplete; cached work is preserved.\n")
            return
        if args.command == "run-bm25":
            print(
                json.dumps(run_bm25(args.bundle, args.output), sort_keys=True, indent=2)
            )
            return
        if args.command == "build-benchmark":
            print(
                json.dumps(
                    build_benchmark(args.corpus, args.inputs, args.config, args.output),
                    sort_keys=True,
                    indent=2,
                )
            )
            return
        if args.command == "validate-benchmark":
            print(json.dumps(validate_benchmark(args.bundle), sort_keys=True, indent=2))
            return
        if args.command == "inspect-corpus":
            print(json.dumps(inspect_corpus(args.path), sort_keys=True, indent=2))
            return
        if args.command == "ingest-corpus":
            try:
                query = CorpusQuery(
                    court=args.court,
                    filed_after=args.filed_after,
                    filed_before=args.filed_before,
                    max_cases=args.max_cases,
                )
            except ValidationError:
                parser.error("Invalid corpus filters.")
            if (
                query.filed_after
                and query.filed_before
                and query.filed_after > query.filed_before
            ):
                parser.error("Filed-date range is reversed.")
            manifest = ingest_corpus(
                query,
                args.output,
                courtlistener_token(),
                request_interval=args.request_interval,
                resume=args.resume,
                transport=transport,
            )
            print(
                json.dumps(
                    {
                        "status": manifest.runs[-1].status,
                        "ingestion_quality": manifest.runs[-1].quality(),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return
        token = courtlistener_token()
        case = normalize_case(*fetch_case(args.cluster_id, token, transport=transport))
    except (
        IngestionError,
        ConfigurationError,
        CorpusError,
        BenchmarkError,
        RetrievalError,
        GraphError,
        ResearchError,
    ) as error:
        parser.exit(1, f"Error: {error}\n")
    except KeyboardInterrupt:
        parser.exit(130, "Interrupted; completed corpus records are preserved.\n")
    print(case.model_dump_json(indent=2))

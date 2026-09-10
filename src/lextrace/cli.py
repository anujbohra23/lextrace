"""LexTrace command-line entry point."""

import argparse
from collections.abc import Sequence

import httpx

from lextrace.config import ConfigurationError, courtlistener_token
from lextrace.ingestion.courtlistener import IngestionError, fetch_case
from lextrace.ingestion.normalize import normalize_case


def _cluster_id(value: str) -> int:
    if not value.isascii() or not value.isdecimal() or int(value) <= 0:
        raise argparse.ArgumentTypeError("cluster_id must be a positive integer")
    return int(value)


def main(
    argv: Sequence[str] | None = None, *, transport: httpx.BaseTransport | None = None
) -> None:
    """Ingest one decision cluster and print its complete normalized JSON."""
    parser = argparse.ArgumentParser(description="LexTrace research tools")
    commands = parser.add_subparsers(dest="command")
    ingest = commands.add_parser("ingest-case", help="Ingest a CourtListener cluster")
    ingest.add_argument("cluster_id", type=_cluster_id)
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return
    try:
        token = courtlistener_token()
        case = normalize_case(*fetch_case(args.cluster_id, token, transport=transport))
    except (IngestionError, ConfigurationError) as error:
        parser.exit(1, f"Error: {error}\n")
    print(case.model_dump_json(indent=2))

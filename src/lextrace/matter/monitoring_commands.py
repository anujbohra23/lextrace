"""Offline, versioned corpus/index/graph publishing for manual monitoring."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pydantic import ValidationError

from lextrace.corpus import CorpusError, atomic_write, read_cases
from lextrace.graph.build import build_graph
from lextrace.graph.contracts import GraphEvidenceBundle
from lextrace.matter.monitoring_corpus import (
    merge_corpus,
    merge_graph_evidence,
    publish_corpus,
)
from lextrace.retrieval.documents import Corpus
from lextrace.retrieval.index import build_index


def add_command(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    update = commands.add_parser(
        "update-corpus", help="Publish a new versioned Case corpus and rebuild indexes"
    )
    update.add_argument("--base", type=Path, required=True)
    update.add_argument("--batch", type=Path, required=True)
    update.add_argument("--output", type=Path, required=True)
    update.add_argument("--index-output", type=Path, required=True)
    update.add_argument("--bm25-only", action="store_true")
    update.add_argument("--old-citation-source", type=Path)
    update.add_argument("--new-citation-source", type=Path)
    update.add_argument("--graph-output", type=Path)


def run_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> bool:
    if args.command != "update-corpus":
        return False
    graph_fields = (
        args.old_citation_source,
        args.new_citation_source,
        args.graph_output,
    )
    if any(field is not None for field in graph_fields) and not all(
        field is not None for field in graph_fields
    ):
        parser.error("Graph update requires both citation sources and --graph-output.")
    old = Corpus.load(args.base)
    merged, delta = merge_corpus(old, read_cases(args.batch))
    publish_corpus(merged, args.output)
    index = build_index(args.output, args.index_output, dense=not args.bm25_only)
    graph_id: str | None = None
    if args.graph_output is not None:
        try:
            old_bundle = GraphEvidenceBundle.model_validate_json(
                args.old_citation_source.read_bytes()
            )
            new_bundle = GraphEvidenceBundle.model_validate_json(
                args.new_citation_source.read_bytes()
            )
        except (OSError, ValidationError):
            raise CorpusError(
                "Could not read valid citation update evidence."
            ) from None
        combined = merge_graph_evidence(old_bundle, new_bundle)
        source_path = args.output.with_suffix(".citations.json")
        if not source_path.resolve().is_relative_to(Path("data").resolve()):
            raise CorpusError("Graph evidence output must be under data/.")
        serialized = combined.model_dump_json(indent=2) + "\n"
        if source_path.exists():
            if source_path.read_text(encoding="utf-8") != serialized:
                raise CorpusError("Existing graph evidence differs.")
        else:
            atomic_write(source_path, serialized)
        graph_id = build_graph(args.output, source_path, args.graph_output).graph_id
    print(
        json.dumps(
            {
                "old_version": delta.old_version,
                "new_version": delta.new_version,
                "added_case_ids": delta.added_case_ids,
                "metadata_changed_case_ids": delta.metadata_changed_case_ids,
                "text_changed_case_ids": delta.text_changed_case_ids,
                "index_identity": index.corpus_hash,
                "graph_identity": graph_id,
                "index_strategy": "full_atomic_rebuild",
            },
            sort_keys=True,
            indent=2,
        )
    )
    return True

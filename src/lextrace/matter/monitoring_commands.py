"""Offline, versioned corpus/index/graph publishing for manual monitoring."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import httpx
from pydantic import ValidationError

from lextrace.config import AppSettings, courtlistener_token
from lextrace.corpus import CorpusError, CorpusQuery, atomic_write, read_cases
from lextrace.graph.build import build_graph
from lextrace.graph.contracts import GraphEvidenceBundle
from lextrace.graph.store import CitationGraph
from lextrace.ingestion.corpus import ingest_corpus
from lextrace.matter.monitoring import MonitorService
from lextrace.matter.monitoring_contracts import CorpusDelta, MonitoringLimits
from lextrace.matter.monitoring_corpus import (
    merge_corpus,
    merge_graph_evidence,
    publish_corpus,
)
from lextrace.matter.monitoring_judge import StructuredImpactJudge
from lextrace.matter.store import MatterStore
from lextrace.research.llm import configured_llm
from lextrace.retrieval.documents import Corpus
from lextrace.retrieval.index import IndexMetadata, LocalIndex, build_index


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
    refresh = commands.add_parser(
        "refresh-monitoring",
        help="Ingest a bounded CourtListener batch, rebuild local views, and monitor",
    )
    refresh.add_argument("--base", type=Path, required=True)
    refresh.add_argument("--court", required=True)
    refresh.add_argument("--filed-after", type=date.fromisoformat)
    refresh.add_argument("--filed-before", type=date.fromisoformat)
    refresh.add_argument("--max-cases", type=int, required=True)
    refresh.add_argument("--request-interval", type=float, required=True)
    refresh.add_argument("--resume", action="store_true")
    refresh.add_argument("--output", type=Path, required=True)
    refresh.add_argument("--index-output", type=Path, required=True)
    refresh.add_argument("--bm25-only", action="store_true")
    refresh.add_argument("--old-index", type=Path)
    refresh.add_argument("--old-graph", type=Path)
    refresh.add_argument("--old-citation-source", type=Path)
    refresh.add_argument("--new-citation-source", type=Path)
    refresh.add_argument("--graph-output", type=Path)
    refresh.add_argument("--matter-id", action="append", default=[])
    refresh.add_argument("--max-llm-calls", type=int, default=0)
    refresh.add_argument("--max-tokens", type=int, default=0)


def _publish_update(
    base: Path,
    batch: Path,
    output: Path,
    index_output: Path,
    *,
    dense: bool,
    old_citation_source: Path | None,
    new_citation_source: Path | None,
    graph_output: Path | None,
    parser: argparse.ArgumentParser,
) -> tuple[Corpus, Corpus, CorpusDelta, IndexMetadata, str | None]:
    graph_fields = (old_citation_source, new_citation_source, graph_output)
    if any(field is not None for field in graph_fields) and not all(
        field is not None for field in graph_fields
    ):
        parser.error("Graph update requires both citation sources and --graph-output.")
    old = Corpus.load(base)
    merged, delta = merge_corpus(old, read_cases(batch))
    publish_corpus(merged, output)
    index = build_index(output, index_output, dense=dense)
    graph_id: str | None = None
    if graph_output is not None:
        assert old_citation_source is not None
        assert new_citation_source is not None
        try:
            old_bundle = GraphEvidenceBundle.model_validate_json(
                old_citation_source.read_bytes()
            )
            new_bundle = GraphEvidenceBundle.model_validate_json(
                new_citation_source.read_bytes()
            )
        except (OSError, ValidationError):
            raise CorpusError(
                "Could not read valid citation update evidence."
            ) from None
        combined = merge_graph_evidence(old_bundle, new_bundle)
        source_path = output.with_suffix(".citations.json")
        if not source_path.resolve().is_relative_to(Path("data").resolve()):
            raise CorpusError("Graph evidence output must be under data/.")
        serialized = combined.model_dump_json(indent=2) + "\n"
        if source_path.exists():
            if source_path.read_text(encoding="utf-8") != serialized:
                raise CorpusError("Existing graph evidence differs.")
        else:
            atomic_write(source_path, serialized)
        graph_id = build_graph(output, source_path, graph_output).graph_id
    return old, merged, delta, index, graph_id


def run_command(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    *,
    transport: httpx.BaseTransport | None = None,
) -> bool:
    if args.command not in {"update-corpus", "refresh-monitoring"}:
        return False
    if args.command == "refresh-monitoring":
        return _refresh(args, parser, transport=transport)
    _, _, delta, index, graph_id = _publish_update(
        args.base,
        args.batch,
        args.output,
        args.index_output,
        dense=not args.bm25_only,
        old_citation_source=args.old_citation_source,
        new_citation_source=args.new_citation_source,
        graph_output=args.graph_output,
        parser=parser,
    )
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


def _refresh(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    *,
    transport: httpx.BaseTransport | None,
) -> bool:
    try:
        query = CorpusQuery(
            court=args.court,
            filed_after=args.filed_after,
            filed_before=args.filed_before,
            max_cases=args.max_cases,
        )
        limits = MonitoringLimits(
            max_llm_calls=args.max_llm_calls, max_tokens=args.max_tokens
        )
    except ValidationError:
        parser.error("Invalid monitoring refresh bounds or corpus filters.")
    if query.max_cases > limits.max_new_cases:
        parser.error("Refresh case count exceeds the monitoring run case limit.")
    if limits.max_llm_calls and limits.max_tokens < 2500:
        parser.error("Monitoring LLM token budget must be at least 2500.")
    settings = AppSettings.from_environment()
    if limits.max_llm_calls and not settings.llm_model:
        raise CorpusError("Monitoring impact model is not configured.")
    judge = (
        StructuredImpactJudge(
            configured_llm(
                settings.llm_model,
                provider=settings.llm_provider,
                openai_base_url=settings.llm_base_url,
                ollama_base_url=settings.ollama_base_url,
                timeout=settings.llm_timeout,
                max_retries=0,
                max_completion_tokens=min(400, limits.max_tokens // 4),
            )
        )
        if settings.llm_model and limits.max_llm_calls
        else None
    )
    batch_path = args.output.with_suffix(".batch.jsonl")
    manifest = ingest_corpus(
        query,
        batch_path,
        courtlistener_token(),
        request_interval=args.request_interval,
        resume=args.resume,
        transport=transport,
    )
    old, new, delta, index, graph_id = _publish_update(
        args.base,
        batch_path,
        args.output,
        args.index_output,
        dense=not args.bm25_only,
        old_citation_source=args.old_citation_source,
        new_citation_source=args.new_citation_source,
        graph_output=args.graph_output,
        parser=parser,
    )
    old_index_identity: str | None = None
    if args.old_index is not None:
        old_index = LocalIndex(args.old_index, corpus_path=args.base)
        old_index_identity = (
            f"{old_index.metadata.corpus_hash}:{old_index.metadata.representation_hash}"
        )
    old_graph = CitationGraph(args.old_graph) if args.old_graph else None
    new_graph = CitationGraph(args.graph_output) if args.graph_output else old_graph
    store = MatterStore(settings.matter_db, settings.private_matter_root)
    try:
        selected = args.matter_id or [
            matter.matter_id for matter in store.list_matters()
        ]
        run = MonitorService(
            store,
            old,
            new,
            old_graph=old_graph,
            new_graph=new_graph,
            judge=judge,
            limits=limits,
            old_index_identity=old_index_identity,
            new_index_identity=f"{index.corpus_hash}:{index.representation_hash}",
        ).run(selected)
    finally:
        store.close()
        if old_graph is not None:
            old_graph.close()
        if new_graph is not None and new_graph is not old_graph:
            new_graph.close()
    print(
        json.dumps(
            {
                "run_id": run.run_id,
                "outcome": run.outcome,
                "old_corpus_version": delta.old_version,
                "new_corpus_version": delta.new_version,
                "old_index_identity": old_index_identity,
                "new_index_identity": run.new_corpus.index_identity,
                "old_graph_identity": run.old_graph_version,
                "new_graph_identity": graph_id,
                "added_case_ids": delta.added_case_ids,
                "alerts_created": run.alerts_created,
                "ingestion_quality": manifest.runs[-1].quality(),
                "index_strategy": "full_atomic_rebuild",
                "graph_strategy": "full_atomic_rebuild" if graph_id else "unchanged",
            },
            sort_keys=True,
            indent=2,
        )
    )
    return True

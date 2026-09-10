"""Offline BM25 experiment runner over a validated reviewed or provisional bundle."""

import json
import math
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from statistics import mean, median

from lextrace.corpus import read_cases
from lextrace.evaluation.benchmark import (
    BenchmarkError,
    BenchmarkInputs,
    BenchmarkItem,
    BenchmarkManifest,
    canonical,
    digest,
)
from lextrace.evaluation.benchmark_build import (
    _check_leakage,
    load_model,
    validate_benchmark,
)
from lextrace.evaluation.retrieval_metrics import macro, metrics
from lextrace.retrieval.bm25 import BM25, BM25Config, tokenize


def run_bm25(
    bundle: Path, output: Path, config: BM25Config | None = None
) -> dict[str, object]:
    config = config or BM25Config()
    started = time.perf_counter()
    validate_benchmark(bundle)
    if (
        not output.resolve().is_relative_to(Path("artifacts").resolve())
        or output.exists()
    ):
        raise BenchmarkError("Use a new run directory under artifacts/.")
    manifest = load_model(bundle / "manifest.json", BenchmarkManifest)
    provenance = load_model(bundle / "provenance.json", BenchmarkInputs)
    cases = read_cases(bundle / "candidates.jsonl")
    items = [
        BenchmarkItem.model_validate_json(line)
        for line in (bundle / "queries.jsonl").read_text().splitlines()
    ]
    settings = {
        "method": "bm25",
        **asdict(config),
        "tokenizer": "unicode-alphanumeric-lowercase-v1",
        "query_tf": "linear",
        "idf": "log(1+(N-df+0.5)/(df+0.5))",
        "index_unit": "case; all opinions in stored order; text only",
        "benchmark_sha256": digest((bundle / "manifest.json").read_bytes()),
        "candidate_sha256": manifest.candidates_sha256,
        "queries_sha256": manifest.queries_sha256,
        "implementation_sha256": digest(
            Path(__file__).read_bytes()
            + (Path(__file__).parent / "retrieval_metrics.py").read_bytes()
            + (Path(__file__).parents[1] / "retrieval/bm25.py").read_bytes()
        ),
    }
    run_id = digest(json.dumps(settings, sort_keys=True))[:16]
    before = time.perf_counter()
    index = BM25(cases, config)
    index_seconds = time.perf_counter() - before
    timings: list[float] = []
    ranking_lines: list[str] = []
    per_query: dict[str, dict[str, float]] = {}
    analysis: list[dict[str, object]] = []
    mappings = {m.opinion_id: m for m in provenance.opinion_mappings}
    evidence = {e.citing_opinion_id: e for e in provenance.citations}
    terms = [
        term
        for m in mappings.values()
        for term in [m.case_name or "", *m.aliases, *m.reporter_citations]
    ]
    for item in items:
        before = time.perf_counter()
        results = index.rank(item.query_id, item.query_text, run_id)
        timings.append(time.perf_counter() - before)
        ids = [result.case_id for result in results]
        if set(ids) != set(manifest.candidate_case_ids):
            raise BenchmarkError("Ranking does not cover the complete candidate pool.")
        ranking_lines.extend(canonical(row) + "\n" for row in results)
        scores = metrics(ids, set(item.positive_case_ids))
        per_query[item.query_id] = scores
        query_terms = set(tokenize(item.query_text))
        missed = []
        for positive in item.positive_case_ids:
            rank = ids.index(positive) + 1
            overlap = len(query_terms & index.documents[positive].keys()) / max(
                1, len(query_terms)
            )
            if rank > 20 or overlap < 0.1:
                depths = [
                    depth
                    for oid, depth in evidence[item.source_opinion_id].depths.items()
                    if mappings[oid].case_id == positive
                ]
                missed.append(
                    {
                        "case_id": positive,
                        "rank": rank,
                        "query_term_overlap": overlap,
                        "citation_depths": depths,
                        "possible_category": "vocabulary mismatch"
                        if overlap < 0.1
                        else "benchmark weak-label ambiguity",
                        "just_outside_top20": 21 <= rank <= 30,
                    }
                )
        leakage = False
        try:
            _check_leakage(item.query_text, terms)
        except BenchmarkError:
            leakage = True
        analysis.append(
            {
                "query_id": item.query_id,
                "recall20_zero": scores["recall@20"] == 0,
                "low_reciprocal_rank": scores["mrr"] < 0.05,
                "potential_leakage": leakage,
                "missed_positives": missed,
                "interpretation": (
                    "Automated descriptive flags, not causal findings "
                    "or legal context labels."
                ),
            }
        )
    aggregate = {
        split: macro(
            [
                per_query[i.query_id]
                for i in items
                if split == "overall" or i.split == split
            ]
        )
        for split in ("dev", "test", "overall")
    }
    ordered = sorted(timings)
    summary: dict[str, object] = {
        "run_id": run_id,
        "status": "PROVISIONAL"
        if manifest.review_status == "review_required"
        else "REVIEWED",
        "query_count": len(items),
        "split_query_counts": {
            split: sum(item.split == split for item in items)
            for split in ("dev", "test")
        },
        "candidate_count": len(cases),
        "mean_positives": mean(len(i.positive_case_ids) for i in items),
        "median_positives": median(len(i.positive_case_ids) for i in items),
        "metrics": aggregate,
        "per_query": per_query,
        "index_seconds": index_seconds,
        "runtime_seconds": time.perf_counter() - started,
        "query_latency_seconds": {
            "mean": mean(timings),
            "p50": median(timings),
            "p95": ordered[math.ceil(0.95 * len(ordered)) - 1],
        },
        "labels": "citation-derived weak positives; other candidates unjudged",
    }
    lengths = sorted(index.lengths, key=lambda key: (index.lengths[key], int(key)))
    errors = {
        "queries": analysis,
        "shortest_documents": {k: index.lengths[k] for k in lengths[:5]},
        "longest_documents": {k: index.lengths[k] for k in lengths[-5:]},
        "length_unit": "tokens",
        "context_audit": (
            "Procedural/background/substantive attribution requires human review."
        ),
    }
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
            destination = Path(temporary) / "run"
            destination.mkdir()
            (destination / "rankings.jsonl").write_text(
                "".join(ranking_lines), encoding="utf-8"
            )
            for name, value in (
                ("config", settings),
                ("metrics", summary),
                ("error-analysis", errors),
            ):
                (destination / (name + ".json")).write_text(
                    json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2)
                    + "\n",
                    encoding="utf-8",
                )
            destination.rename(output)
    except OSError:
        raise BenchmarkError("Could not write retrieval run.") from None
    return summary

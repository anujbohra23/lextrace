"""Evaluate engine rankings using the existing binary citation metrics."""

import json
import math
import tempfile
from collections.abc import Sequence
from pathlib import Path
from statistics import mean, median

from lextrace.evaluation.benchmark import (
    BenchmarkItem,
    BenchmarkManifest,
    canonical,
    digest,
)
from lextrace.evaluation.benchmark_build import load_model, validate_benchmark
from lextrace.evaluation.retrieval_metrics import macro, metrics
from lextrace.retrieval.bm25 import RankedResult
from lextrace.retrieval.contracts import Mode, RetrievalError, SearchRequest
from lextrace.retrieval.engine import LexTraceRetriever


def evaluate_engine(
    bundle: Path,
    index: Path,
    output: Path,
    *,
    modes: Sequence[Mode] = ("bm25", "dense", "hybrid", "reranked"),
    engine: LexTraceRetriever | None = None,
) -> dict[str, object]:
    validate_benchmark(bundle)
    if output.exists() or not output.resolve().is_relative_to(
        Path("artifacts").resolve()
    ):
        raise RetrievalError("Use a new evaluation directory under artifacts/.")
    if not modes or len(set(modes)) != len(modes):
        raise RetrievalError("Specify distinct evaluation modes.")
    retriever = engine or LexTraceRetriever.from_index(index)
    manifest = load_model(bundle / "manifest.json", BenchmarkManifest)
    if retriever.corpus.hash != manifest.candidates_sha256:
        raise RetrievalError("Index corpus differs from benchmark candidates.")
    items = [
        BenchmarkItem.model_validate_json(line)
        for line in (bundle / "queries.jsonl").read_text().splitlines()
    ]
    config = {
        "engine": retriever.config.model_dump(mode="json"),
        "benchmark_sha256": digest((bundle / "manifest.json").read_bytes()),
        "modes": list(modes),
        "output_depth": 100,
        "ranking_scope": (
            "Configured candidate depths and at most 100 results; "
            "absent positives receive zero gain."
        ),
    }
    run_id = digest(json.dumps(config, sort_keys=True))[:16]
    summaries: dict[str, object] = {}
    rows: list[str] = []
    traces = []
    try:
        for mode in modes:
            per_query: dict[str, dict[str, float]] = {}
            timings = []
            for item in items:
                response = retriever.search_response(
                    SearchRequest(
                        query=item.query_text,
                        top_k=min(100, len(retriever.corpus.ids)),
                        mode=mode,
                    )
                )
                per_query[item.query_id] = metrics(
                    [r.case_id for r in response.results], set(item.positive_case_ids)
                )
                timings.append(response.trace.stage_seconds["total"])
                traces.append(
                    {"query_id": item.query_id, **response.trace.model_dump()}
                )
                rows.extend(
                    canonical(
                        RankedResult(
                            query_id=item.query_id,
                            case_id=r.case_id,
                            rank=r.rank,
                            score=r.final_score,
                            method=mode,
                            run_id=run_id,
                        )
                    )
                    + "\n"
                    for r in response.results
                )
            ordered = sorted(timings)
            summaries[mode] = {
                "per_query": per_query,
                "aggregate": {
                    split: macro(
                        [
                            per_query[i.query_id]
                            for i in items
                            if split == "overall" or i.split == split
                        ]
                    )
                    for split in ("dev", "test", "overall")
                },
                "latency_seconds": {
                    "mean": mean(timings),
                    "p50": median(timings),
                    "p95": ordered[math.ceil(0.95 * len(ordered)) - 1],
                },
            }
        result: dict[str, object] = {
            "run_id": run_id,
            "status": "PROVISIONAL"
            if manifest.review_status == "review_required"
            else "REVIEWED",
            "query_count": len(items),
            "candidate_count": len(retriever.corpus.ids),
            "mean_positives": mean(len(i.positive_case_ids) for i in items),
            "median_positives": median(len(i.positive_case_ids) for i in items),
            "modes": summaries,
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
            folder = Path(temporary) / "evaluation"
            folder.mkdir()
            (folder / "rankings.jsonl").write_text("".join(rows), encoding="utf-8")
            for name, value in [
                ("config", config),
                ("metrics", result),
                ("traces", traces),
            ]:
                (folder / (name + ".json")).write_text(
                    json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8"
                )
            folder.rename(output)
        return result
    except OSError:
        raise RetrievalError("Could not write evaluation artifacts.") from None
    finally:
        if engine is None:
            retriever.close()

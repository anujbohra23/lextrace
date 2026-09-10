# Citation recovery and BM25 baseline

## Implementation

The dependency-free baseline implements Okapi BM25 with `k1=1.2`, `b=0.75`, and
`idf(t)=ln(1+(N-df(t)+0.5)/(df(t)+0.5))`. Query term frequencies multiply the
contribution linearly. This small implementation makes the scoring contract
inspectable and avoids an additional library; hand-calculated tests verify it.

The indexing unit is a Case/cluster. All opinion texts are concatenated in stored
order, including dissents/concurrences; names, reporter metadata, query identities,
and citation relationships are not indexed. Unicode alphanumeric tokens are
lowercased. There is no stemming, stopword removal, synonym expansion, or canonical
text rewriting. Ties use ascending numeric cluster ID. Empty-token documents and
duplicate IDs fail. Every query ranks the complete candidate set exactly once.

`run-bm25` validates the entire benchmark bundle first. Only `query_text` reaches
the scorer. Output directories under `artifacts/` must be new and contain:

- `rankings.jsonl`: typed query/case IDs, rank, finite score, method, run ID.
- `config.json`: parameters, tokenizer, source/code fingerprints, benchmark hashes.
- `metrics.json`: per-query and dev/test/overall macro scores, positive counts,
  runtime/index time, and mean/median/nearest-rank-p95 query latency in seconds.
- `error-analysis.json`: zero Recall@20, reciprocal rank below 0.05, positives at
  ranks 21–30, low query-token overlap, leakage flags, and document length extremes.

Recall@K divides recovered positives by all eligible citation-derived positives.
MRR is reciprocal first-positive rank. Binary NDCG@10 divides discounted gain
by the ideal top-ten gain for that query's positive count. Other candidates have
zero computational gain but remain **unjudged**. Provisional bundle status applies
to all reported splits, particularly test; automated flags do not establish legal
relevance, citation context, or causes of retrieval failure.

## Live acquisition report — 2026-09-10

The previous broad joined cluster-list requests exceeded a 30-second client
timeout. Their server-side root cause was not observed, so database/query-cost
explanations remain hypotheses. The successful replacement used published
nonsemantic Search with court/date filters and filing-date order. It returned
642 records in 3.44 seconds. Some results represent duplicate decisions, reinforcing
the need for litigation/duplicate review. One exploratory Search parameter set
returned zero records before the documented filter names were used.

Sources: [Search API](https://wiki.free.law/c/courtlistener/help/api/rest/v4/search),
REST `/opinions/{id}/`, `/clusters/{id}/`, `/dockets/{id}/`, paginated
`/opinions-cited/?citing_opinion=...`, and `/api-usage/`.
The existing five-case M2 corpus supplied usable query text; the pilot verified
selected opinion metadata against REST and acquired all outgoing edge pages.

| Query cluster / selected opinion | Case | Filing date | Edges | Pages | Mapped clusters | Valid positives so far | Eligibility pending |
|---|---|---|---:|---:|---:|---:|---:|
| 41 / 41 | Redd v. Wright | 2010-03-10 | 20 | 1 | 2 | 1 | 19 |
| 39 / 39 | United States v. Awad | 2010-03-11 | 10 | 1 | 0 | 0 | 10 |
| 40 / 40 | Huth v. Haslun | 2010-03-11 | 14 | 1 | 2 | 2 | 12 |
| 37 / 37 | United States v. Deandrade | 2010-03-12 | 19 | 1 | 2 | 2 | 17 |
| 38 / 38 | Alexander v. Cahill | 2010-03-12 | 38 | 2 | 6 | 2 | 34 |

All selected types were `010combined`. The 101 edges contain 100 globally unique
cited opinion IDs; observed depths range from 1 to 55. Depth is a mention count,
not a relevance grade. The schema accepts nonnegative integers and rejects negative,
boolean, and string depths. Pagination completed without repeated cursors.

Twelve of 100 opinion IDs were resolved to cluster IDs (all 12 attempted mappings
succeeded); 88 were not attempted before the request/quota limits. Nine mapped
cases were fully normalized: seven eligible ca2/scotus candidates and two excluded
for court (`ca5`, `ca11`). Three additional mappings lack complete eligibility/text
checks. Thus **91 eligibility checks remain pending**, not failed or excluded.
The provisional positive counts above are lower bounds, not complete labels.
Three queries already have two eligible positives, but the five-query viability
check and positive-count distribution remain incomplete.

Direct REST parent relationships and cluster opinion membership confirmed:

- Opinion `9849069` → cluster `1275095` (Humphrey, 1985-11-13).
- Opinion `9670757` → cluster `1707195` (Humphrey, 1984-09-19).
- Opinion `9631565` → cluster `1444444` (Koger v. Bryan, 2008-04-24).

These examples prove that opinion/cluster equality cannot be assumed, even though
many older records happen to share IDs. Full source text was not printed.

## Cost and external blocker

This run used **50 authenticated API requests**, including two quota checks,
and three public S3 metadata listings. There were no HTTP 429 or API/schema errors.
The first pilot stopped at its deliberate 40-request ceiling. After the six-request
mapping check, the full acquisition command consulted quota and stopped before
further requests: **50/50 hourly requests used, 0 remaining; 75/125 daily used,
50 remaining**. Completed pilot resources are preserved in ignored local caches.

The pilot cost was roughly ten API requests/query, despite mapping only 12% of
the distinct citation targets. A 300-case corpus ordinarily needs approximately
900 detail requests (cluster, docket, one opinion), plus discovery/citation calls;
cache overlap lowers this, while multiple opinions increase it. The current
account cannot finish that REST workload in one session. Higher quota or several
resumable acquisition windows are needed; the implementation does not wait out
limits, retry aggressively, or conceal incompleteness.

The official [bulk data](https://wiki.free.law/c/courtlistener/help/api/bulk-data/bulk-legal-data)
listing showed `citation-map-2026-06-30.csv.bz2` at 526,206,545 bytes. It was not
downloaded: six REST calls already completed the pilot edges, and bulk edges
would not supply the bottleneck opinion text/cluster/docket metadata. Importing
a whole text replica is disproportionate to this milestone. No embedding files
or database infrastructure were downloaded or introduced.

## Results and next experiment

There are five real pilot query cases and seven confirmed eligible candidates,
not a 25-query/200–500-case benchmark. No complete candidate or reviewed/frozen
bundle exists. Human review has not occurred. Five real 260-word excerpt proposals were saved
locally with source offsets/hashes and explicit incomplete citation-leakage coverage;
they remain review_required and are not complete BenchmarkItems.
**No real V1 BM25 experiment was
run**, so Recall@5/10/20, MRR, NDCG@10, latency, and failure-category frequencies
are unavailable. Synthetic end-to-end tests demonstrate execution only; they are
not reported as scientific results. The design's target counts/cutoff remain
unchanged; insufficient quota does not justify shrinking the benchmark afterthought.

Next operational step: resume acquisition with sufficient quota, complete every
citation mapping and eligibility check, build a review-ready bundle, and perform
human leakage/context audits. Then run the pinned BM25 baseline once. The next
scientific experiment is **dense retrieval versus this fixed BM25 configuration
on the identical candidate set, query text, splits, and binary citation labels**,
with Recall@5/10/20, MRR, NDCG@10, and latency reported together. Select any dense
configuration using dev only, freeze it, then evaluate test. Dense implementation,
reranking, authority ranking, LLMs, and database work remain deferred.


## Implementation inventory and validation

Added source files:
`ingestion/benchmark_acquisition.py`, `ingestion/benchmark_job.py`,
`evaluation/excerpts.py`, `retrieval/bm25.py`,
`evaluation/retrieval_metrics.py`, and `evaluation/retrieval_run.py`
(all under `src/lextrace/`). Modified `cli.py`, `evaluation/benchmark.py`,
`evaluation/benchmark_build.py`, and `ingestion/benchmark_sources.py`.
The approved Case model and source-text normalizer were not changed.

Added tests: `tests/unit/test_acquisition.py`, `tests/unit/test_retrieval.py`,
`tests/integration/test_benchmark_acquisition.py`, and
`tests/integration/test_retrieval_run.py`. Updated the existing bundle file-set
assertion in `tests/integration/test_benchmark_build.py`. Other changes are
`AGENTS.md`, `README.md`, `docs/architecture.md`, `docs/benchmark-v1.md`, this report,
and `experiments/configs/benchmark_v1.json`.

Validation: 212 tests passed; Ruff lint/format, strict mypy, pre-commit, and
`git diff --check` passed. Two upstream Starlette deprecation warnings remain.
The local macOS editable `.pth` UF_HIDDEN flag recurred after clearing; validation
used an explicit source path. No application or packaging workaround was added.

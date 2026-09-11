# LexTrace

Research foundations for an authority-aware legal precedent retrieval and
reasoning system for U.S. case law. LexTrace fetches individual CourtListener decision clusters and builds small
local JSONL corpora, preserving separate opinions. No database is used.

## Setup

Use Python 3.12:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,retrieval]'
pre-commit install
```

The health API and CLI help require no credentials. Ingestion requires a
CourtListener API token exported as `COURTLISTENER_API_TOKEN`. See `.env.example`;
`.env` files are not automatically loaded.

## Run locally

```sh
uvicorn lextrace.api.app:app --reload
curl http://127.0.0.1:8000/health
lextrace --help
```

The endpoint returns `{"status":"ok"}`.

## Ingest one case

```sh
export COURTLISTENER_API_TOKEN='<your-token>'
lextrace ingest-case 2812209
```

The argument is a positive integer **cluster ID**, as found in CourtListener's
public `/opinion/<cluster_id>/...` URLs, not an individual opinion ID or citation.
The command fetches the cluster, its docket, and each linked opinion sequentially.
It prints complete indented JSON to stdout, including opinion text. Errors go to
stderr with a nonzero exit code; no partial case is printed.

HTML with citations is preferred and converted to plain text; `plain_text` is the
fallback. Missing text fails ingestion. Opinion types are trimmed source strings,
with `unknown` for missing/blank types. Dates and docket numbers may be null.
The court identifier comes directly from the docket. All API requests use the
fixed CourtListener origin with a 30-second timeout and no redirects or retries.
Tests use mocked HTTPX transport and require no token or network access.

## Development checks

```sh
pytest
ruff check .
ruff format --check .
mypy
pre-commit run --all-files
```

Run `ruff format .` to format code. Activate the development virtual environment
before using pre-commit; its local mypy hook uses installed project dependencies.
Pre-commit checks tracked files, so stage new files before running it manually.

## Layout

Application code lives in `src/lextrace/`, tests in `tests/`, and versioned
experiment configurations in `experiments/configs/`. Local corpora belong in
`data/` and generated results in `artifacts/`; both are ignored except for their
directory placeholders. See [architecture](docs/architecture.md) for module
boundaries and [contributor guidelines](AGENTS.md) for working conventions.

## Build a bounded corpus

```sh
lextrace ingest-corpus --court ca2 --filed-after 2020-01-01 \
  --filed-before 2020-12-31 --max-cases 500 \
  --request-interval 15 --output data/ca2_sample.jsonl
lextrace ingest-corpus --court ca2 --filed-after 2020-01-01 \
  --filed-before 2020-12-31 --max-cases 500 \
  --request-interval 15 --output data/ca2_sample.jsonl --resume
lextrace inspect-corpus data/ca2_sample.jsonl
```

`--request-interval` is required, in seconds: choose conservative pacing for your
account limits (15 above is an example, not a built-in default). Requests are
sequential, including docket/opinion detail requests. HTTP 429 stops the run,
preserves completed cases, and reports a safely parsed Retry-After if supplied.
There are no automatic retries. Other request failures likewise stop safely.

Dates are inclusive cluster filing dates. `--max-cases` bounds the total valid,
unique cases, including saved cases on resume. It does not bound rejected source
records or HTTP requests. A case may have several opinions. Source exhaustion
can produce fewer cases than requested and is reported as `exhausted`.

Every JSONL line is a validated Case, with canonical key ordering and cases sorted
by numeric source ID. Reporter citations identify the decision: a list indicates
known metadata, `[]` means none, and `null` means unknown. This is not a cited-case
graph. Opinion text passes through the approved normalizer without spelling,
OCR, or source-quality corrections.

The adjacent `.manifest.json` records query/version metadata and per-run ingestion
quality: encountered source records, successful new cases, rejections with safe
reason codes, and skipped duplicates. Repeated records count as encounters, so
per-run counts include replay during resume. Malformed records are rejected and
never written into the canonical Case corpus. Request failures are not counted
as malformed-record rejections; their run status is `failed`.

Existing output is protected unless `--resume` is specified. Resume requires the
same query and target count, replays pages, and skips saved IDs before fetching
opinion text. It retries previously rejected records on the next invocation.
An already complete corpus makes no HTTP requests. Use a new path for a different
query; automatic refresh or merging is not supported. Use one writer per output.

The corpus is replaced atomically after each successful case. The manifest is
updated separately; JSONL is authoritative after interruption. Resume reconciles
the most recent interrupted run's saved-case count. Metadata counts otherwise
reflect the last saved progress, not a transactional audit log. Retain the output
for reproducibility: CourtListener can update records between fresh downloads.

`inspect-corpus` is offline and reports only normalized corpus quality: total
cases, reporter/docket completeness, duplicates, courts, date range, and per-opinion
Unicode character-length statistics (nearest-rank p95). It rejects malformed
JSONL with a line number instead of treating invalid input as a valid Case.
Valid cases already require usable text, so missing-text counts are not reported.
Both corpus and manifest files under `data/` are ignored by Git.

## Citation-Recovery Benchmark and BM25

V1 targets 25 Second Circuit query decisions from 2010 and 200–500 earlier
Second Circuit/Supreme Court candidates. Citation-derived positives are weak
labels; other candidates are unjudged. Automated excerpts remain review_required.
The live pilot succeeded, but account quota blocked full acquisition; no real
V1 BM25 scores are available yet. See [the protocol](docs/benchmark-v1.md) and
[the live acquisition and baseline report](docs/retrieval-baseline.md).

```sh
# Token must already be in the environment; this command does not load .env.
lextrace acquire-benchmark --output data/benchmarks/v1/acquisition \
  --request-interval 15 --timeout 90 --max-requests 40
# Reinvoke the same command to resume from the hash-checked cache.
# Only build once the required query/candidate counts and evidence are complete.
lextrace build-benchmark --corpus data/benchmarks/v1/acquisition/sources.jsonl \
  --inputs data/benchmarks/v1/acquisition/inputs.json \
  --config experiments/configs/benchmark_v1.json \
  --output data/benchmarks/v1/candidate
lextrace validate-benchmark data/benchmarks/v1/candidate
lextrace run-bm25 data/benchmarks/v1/candidate --output artifacts/bm25-v1
```

Only acquisition reads credentials. Build, validation, and BM25 are offline.
BM25 indexes candidate opinion text only and ranks every candidate for each
query. Outputs contain rankings, macro/per-query metrics, latency, hashes, and
automated error flags. Human-unreviewed results are labeled PROVISIONAL.
Generated data and experiment outputs are ignored by Git.

## Retrieval Engine v1

The local retrieval engine searches any normalized Case JSONL corpus without a
database. Build an index once, inspect its identity, and search it through one
contract:

```sh
lextrace build-index --corpus data/cases.jsonl \
  --output artifacts/indexes/default \
  --config experiments/configs/retrieval_engine_v1.json
lextrace index-info artifacts/indexes/default
lextrace search "employee fired after discussing salary with coworkers" \
  --mode reranked --top-k 10 --json
lextrace search "search and seizure" --mode hybrid --court ca2 \
  --filed-after 2000-01-01 --filed-before 2010-12-31
```

`build-index --lexical-only` creates a BM25-only index. Search modes are `bm25`,
`dense`, `hybrid`, and `reranked`. Human output contains rank, case metadata,
reporter citations, score, one evidence passage, its source URL, diagnostics,
and stage timings; `--json` returns the typed SearchResponse. Canonical opinion
text is never rewritten. Evidence passages retain the opinion ID, exact character
offsets, and a stable content-derived ID.

```mermaid
flowchart TD
    CL[CourtListener] --> C[Normalized Case corpus]
    C --> B[BM25]
    C --> D[Dense retrieval]
    B --> R[Reciprocal rank fusion]
    D --> R
    R --> X[Cross-encoder reranking]
    X --> E[Evidence passage]
    E --> U[CLI / FastAPI]
```

BM25 uses the existing lowercase Unicode-alphanumeric tokenizer and Okapi
scoring. Dense search uses exact blockwise cosine scoring over memory-mapped
NumPy vectors. Hybrid search applies deterministic reciprocal rank fusion with
`k=60`; reranked search applies the cross-encoder to the two strongest lexical
passages from each shortlisted case. Ties resolve by numeric source ID and
duplicate case IDs are removed.

The embedding model is
[`sentence-transformers/all-MiniLM-L6-v2`](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2)
at revision `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`. The reranker is
[`cross-encoder/ms-marco-MiniLM-L6-v2`](https://huggingface.co/cross-encoder/ms-marco-MiniLM-L6-v2)
at revision `233902d25c440f23af6f7d6e94d2946bac0bee0a`. These compact English
MiniLM models were selected for practical local CPU inference; neither is a
legal-domain model. Models are downloaded to ignored `artifacts/models/` on first
use. CPU is the supported default. Configured MPS use falls back to CPU when MPS
is unavailable; inference failures remain explicit rather than changing ranking
methods silently.

Long passages are tokenized into deterministic 256-token windows with 32-token
overlap. Token IDs pass directly to the model, so tokenizer overflow boundaries
do not lose or re-tokenize text. Each window embedding is unit normalized; their
mean is normalized into a passage vector. Passage vectors are averaged and
normalized into the case vector. Window size, overlap, passage size, model and
revision, preprocessing version, corpus hash, and pooling version are part of
index identity. A mismatch or checksum failure requires a new index directory.

Run the API against the default index, or set `LEXTRACE_INDEX_PATH`:

```sh
uvicorn lextrace.api.app:app --reload
curl -X POST http://127.0.0.1:8000/search \
  -H 'content-type: application/json' \
  -d '{"query":"wage retaliation","mode":"hybrid","top_k":5,"filters":{"courts":["ca2"]}}'
curl http://127.0.0.1:8000/cases/2812209
```

`GET /health` never initializes an index. `POST /search` lazily loads one shared
engine per process, and `GET /cases/{source_id}` returns its canonical Case or
404. Invalid requests receive FastAPI validation errors; safe index/model errors
return 503.

Each search emits a structured trace containing a request ID, mode, corpus hash,
index version, query length (never query text), candidate counts, queue and
per-stage seconds, total latency, and pinned model identities. The engine can be
evaluated offline with:

```sh
lextrace evaluate-engine data/benchmarks/v1/candidate \
  --index artifacts/indexes/default --output artifacts/engine-eval-v1 \
  --modes bm25 dense hybrid reranked
```

The adapter validates benchmark/corpus hashes and reports Recall@5/10/20, MRR,
NDCG@10, and mean/p50/p95 latency. Human-unreviewed bundles stay PROVISIONAL;
fixture metrics are tests, not retrieval-quality evidence.

The engineering smoke test used 14 previously saved real CourtListener cases.
It verified all modes, API lifecycle, filters, exact evidence offsets, and warm
local inference. It does not establish retrieval quality: the sample is too small
and several illustrative queries have no clearly responsive case. Exact NumPy
search is intentionally simple and scales linearly; BM25 text and corpus records
also remain in memory. The cross-encoder sees only selected 128-word passages,
and lexical passage selection can miss semantically relevant evidence.

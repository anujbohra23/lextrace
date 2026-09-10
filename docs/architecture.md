# Architecture

LexTrace is one Python package with a health-only FastAPI application and a
synchronous CLI ingestion flows. Database persistence and retrieval remain deferred.

## Implemented ingestion flow

`lextrace ingest-case <cluster_id>` validates a positive integer, lazily reads
`COURTLISTENER_API_TOKEN`, fetches the cluster/docket/opinions, normalizes their
validated response models, and prints a complete internal Case as indented JSON.

- `domain/case.py`: internal Case and Opinion validation and serialization.
- `ingestion/courtlistener.py`: separate external schemas and HTTPX fetching.
- `ingestion/normalize.py`: pure metadata and HTML/plain-text conversion.
- `config.py`: static API base/timeout and ingestion-only credential loading.
- `cli.py`: argument handling, orchestration, JSON output, and safe errors.

A Case represents one decision cluster, with distinct Opinion records. Court IDs
come from docket.court_id. Unknown opinion types remain trimmed source strings;
missing types become `unknown`. No authority classification is inferred.

Validate linked docket/opinion IDs and construct requests against the fixed API
base. Validate the cluster's relative public path before joining it to the fixed
CourtListener origin. Never send credentials to response-provided URLs. Requests
are sequential with explicit timeouts, no redirects, and no retries.

Prefer html_with_citations, removing tags and script/style content while retaining
paragraph boundaries; fall back to plain_text. Missing opinion text fails the
whole command. Errors omit response bodies, credentials, and exception details.

## Research and testing

Unit tests cover models, schemas, links, and normalization. CLI integration tests
exercise HTTPX MockTransport; no external calls or real credentials are needed.
The API health contract remains independent of ingestion credentials.

Version experiment configurations in experiments/configs/. Keep local corpora in
data/ and results in artifacts/, both ignored. Small synthetic responses live in
tests/fixtures/. Ranking, retrieval, and evaluation packages remain placeholders.

## Deferred infrastructure

No database, embeddings, vector store, LLM, RAG, agents, or frontend is included.

## Milestone 2: local corpora

`ingestion/corpus.py` coordinates a bounded sequential run. The CourtListener
client lists clusters in ascending ID order, validates each record independently,
and fetches docket/opinion details through one paced client. Pagination links are
validated and only the cursor is copied into a fixed-origin request with the
original filters. Request failures stop; invalid source records are rejected and
recorded using fixed reason codes. Single-case ingestion retains its no-retry
contract and existing text normalization.

`domain/case.py` adds optional reporter_citations to the existing Case. The
CourtListener cluster's structured reporter metadata is formatted as strings;
unknown metadata stays null. There is no second normalized case representation.

`corpus.py` owns canonical JSONL, atomic file replacement, and a versioned
manifest with per-run counters/rejection metadata. Each saved corpus contains
only valid unique cases. Resume validates query compatibility, reads saved IDs,
then replays pagination. Completed records are never refreshed implicitly.
The corpus and manifest are individually atomic, not one transaction; interrupted
success counts are reconciled against saved records on resume. Single writer only.

`evaluation/corpus_quality.py` reads validated JSONL offline and computes corpus
metrics. Ingestion-quality metrics belong in manifest runs and do not alter the
meaning of corpus completeness. Invalid JSONL produces a safe line-number error.
No source-record payloads or credentials are stored in manifests.

All new ingestion tests use HTTPX MockTransport and injected timing functions.
Local output tests use temporary directories; no external data is needed.

## Milestone 3A: Citation-Recovery Benchmark

`evaluation/benchmark.py` defines benchmark records separately from Case objects.
`evaluation/benchmark_build.py` validates frozen citation mappings, reviewed query
spans, positive coverage, temporal bounds, and audit/split requirements; it samples
unjudged candidates by court/year using seeded hashes and publishes a reproducible
bundle. `ingestion/benchmark_sources.py` parses frozen external metadata only.
It performs no acquisition. Existing ingestion and text normalization are intact.

Only candidates.jsonl is eligible for a future retrieval index. Full source cases,
weak labels, excerpt removals, and audits are evaluation provenance, not retriever
inputs. Reporter citations remain distinct from opinion-to-opinion relationships.
See [the V1 protocol](benchmark-v1.md) for limitations and deferred metric definitions.

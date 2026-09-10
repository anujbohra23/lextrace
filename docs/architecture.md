# Architecture

LexTrace is one Python package with a health-only FastAPI application and a
synchronous CLI ingestion flow. Persistence and retrieval remain deferred.

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

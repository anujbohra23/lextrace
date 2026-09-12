# Engineering v1 Operations

## Configuration and privacy

Runtime configuration is read through `AppSettings`. Copy `.env.example` values
into your process environment; LexTrace does not load `.env` automatically.
`LEXTRACE_PERSIST_CONTENT=true` stores raw questions and complete structured
responses in the local runtime database so completed runs can be displayed and
failed runs retried. Set it to `false` when question privacy matters more than
resume/result lookup. Hashes, status, counts, trace metadata, and errors remain.
Never share or commit the runtime volume.

Pricing is deliberately unknown by default. Copy the versioned pricing example,
fill it only for an exact provider model and date/version, and treat estimates as
operational metadata. Provider-reported cached tokens are tracked separately.

## Demo preparation

Prepare a local normalized corpus using documented ingestion commands. Then:

```sh
lextrace build-index --corpus data/cases.jsonl \
  --output artifacts/indexes/default
lextrace build-citation-graph --corpus data/cases.jsonl \
  --citation-source data/citation-evidence.json \
  --output artifacts/graphs/default
docker compose up --build
```

The backend exposes `/health`, `/ready`, `/search`, case/citation endpoints,
`POST /research`, `GET /research/{run_id}`, its `/trace`, `/runs`, and bounded
`/resume`. Research submission returns 202 and the frontend polls durable state.
The local executor defaults to one workflow at a time to bound memory and cost.

## Release and regression checks

```sh
pytest
ruff check .
ruff format --check .
mypy src tests
pre-commit run --all-files
lextrace evaluate-system --golden artifacts/research-golden-responses.json
cd frontend && npm run lint && npm run typecheck && npm test && npm run build
docker compose config
```

The golden input is a JSON array of complete typed `ResearchResponse` objects
created with deterministic fake providers. Live paid evaluation is manual only.

## Limits

The executor and SQLite stores target a single-host demonstration. Restarting the
process does not recreate already queued in-memory jobs, though their durable
status remains inspectable. Resume restarts a bounded workflow rather than
continuing at an individual LangGraph node. Cache invalidation is exact but the
cache is local and has no eviction policy. CORS and concurrency are configurable;
public authentication and distributed rate limiting are outside Engineering v1.

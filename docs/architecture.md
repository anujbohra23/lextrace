# Architecture

LexTrace starts as one Python package and one FastAPI application. Feature
packages are placeholders, not implemented pipelines.

## Module boundaries

- `domain`: future case, citation, court, authority, and provenance types.
- `ingestion`: future source adapters, parsing, and normalization.
- `retrieval`: future candidate retrieval and retrieval strategies.
- `ranking`: future relevance reranking and legal authority scoring.
- `evaluation`: future reusable dataset loading, metrics, and experiment runs.
- `api`: HTTP entry points; the only route today is `GET /health`.
- `config.py`: static application metadata, with no external configuration yet.
- `cli.py`: help-only entry point for future research commands.

Keep domain code independent of transport concerns. API and CLI entry points
should call reusable feature code. Add cross-module interfaces only when a
concrete use case needs them. Preserve document provenance when ingestion begins,
and distinguish legal authority from semantic relevance when ranking begins.

## Research and testing

Version experiment configurations under `experiments/configs/`. Keep local
datasets and generated outputs in ignored `data/` and `artifacts/` directories.
Use small redistributable examples in `tests/fixtures/`. Unit tests cover isolated
behavior, integration tests cover module interactions, and API tests cover HTTP
contracts. The health route reports process health, not dependency readiness.

## Deferred infrastructure

Persistence, database migrations, orchestration, model integrations, and a
frontend are intentionally deferred. Introduce infrastructure when an actual
feature requires it, preserving a single deployable application where practical.

# Repository Guidelines

## Project Structure & Module Organization

LexTrace is an early research project for authority-aware U.S. case law retrieval.
Use the `src/lextrace/` package layout. Feature packages are `domain`, `ingestion`,
`retrieval`, `ranking`, and `evaluation`; keep them mostly empty until their work
is explicitly scoped. FastAPI code lives in `api/`. `config.py` reads ingestion credentials lazily, and `cli.py` exposes
`lextrace ingest-case <cluster_id>`, `ingest-corpus`, and `inspect-corpus`.
CourtListener schemas live in ingestion;
internal Case and Opinion models live in domain.

Place tests in `tests/unit/`, `tests/integration/`, and `tests/api/`. Store small,
redistributable examples in `tests/fixtures/`, experiment configurations in
`experiments/configs/`, and architecture decisions in `docs/architecture.md`.
Local corpora and run manifests in `data/` and outputs in `artifacts/` are ignored.
Keep reporter citation metadata separate from future cited-case edges. Preserve
source-text defects; do not add correction heuristics. Batch records must validate
as Case objects; safe rejection reasons belong only in run metadata.

## Build, Test, and Development Commands

Use Python 3.12 and an activated virtual environment. Install the package and
development tools with `python -m pip install -e '.[dev]'`.

- `uvicorn lextrace.api.app:app --reload`: start the local API.
- `pytest`: run the test suite.
- `ruff check .`: check lint rules and import ordering.
- `ruff format --check .`: verify formatting; use `ruff format .` to apply it.
- `mypy`: run strict typing checks on source and tests.
- `pre-commit install`: enable hooks; `pre-commit run --all-files` checks tracked files.

## Coding Style & Naming Conventions

Use four-space indentation, 88-character lines, and annotated functions. Prefer
`snake_case` for modules and functions, `PascalCase` for classes, and `UPPER_CASE`
for constants. Ruff handles formatting and import conventions; mypy runs in
strict mode. Keep HTTP handling thin and reusable behavior in feature packages.

## Testing Guidelines

Use pytest with `test_*.py` files and `test_*` functions. Test observable behavior;
API tests use FastAPI's `TestClient`. Add regression tests for meaningful fixes.
There is no coverage percentage requirement yet. Run all three development
checks before submitting changes.

## Commit & Pull Request Guidelines

Follow the scaffold commit convention: short, imperative commit subjects, such
as `Add health endpoint contract test`. Keep changes focused. PR descriptions
should explain purpose, behavior changes, and validation results, and link issues
when applicable.

## Scope & Configuration

Do not add persistence, model services, orchestration, or frontend infrastructure
without an explicit task. Never commit credentials or local corpora. Ingestion requires
`COURTLISTENER_API_TOKEN`; `.env` loading is not configured. Mock external HTTP
with HTTPX MockTransport in tests. Never include credentials in error messages.

## Benchmark Conventions

V1 is a Citation-Recovery Benchmark, not comprehensive relevance ground truth.
Keep its fixed temporal bounds, reviewed query provenance, and complete positive
union intact. Call other candidates unjudged. Benchmark commands are offline;
do not acquire data or add retrieval methods without an explicit task. Keep
source opinion text unchanged and generated artifacts under data/benchmarks/v1/.

# LexTrace

Research foundations for an authority-aware legal precedent retrieval and
reasoning system for U.S. case law. Milestone 1 fetches a single CourtListener decision cluster and prints a
normalized case, preserving separate opinions. No persistence is implemented.

## Setup

Use Python 3.12:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
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

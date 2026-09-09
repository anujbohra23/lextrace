# LexTrace

Research foundations for an authority-aware legal precedent retrieval and
reasoning system for U.S. case law. The initial scaffold contains only a health
endpoint and development tooling; legal processing is not implemented.

## Setup

Use Python 3.12:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
pre-commit install
```

No environment variables or external services are required. `.env.example`
documents this; `.env` files are not automatically loaded.

## Run locally

```sh
uvicorn lextrace.api.app:app --reload
curl http://127.0.0.1:8000/health
lextrace --help
```

The endpoint returns `{"status":"ok"}`. The CLI currently exposes help only.

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

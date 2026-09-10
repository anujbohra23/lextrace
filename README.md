# LexTrace

Research foundations for an authority-aware legal precedent retrieval and
reasoning system for U.S. case law. LexTrace fetches individual CourtListener decision clusters and builds small
local JSONL corpora, preserving separate opinions. No database is used.

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

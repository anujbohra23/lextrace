# User-centered workflows

The usability branch keeps legal source text and model judgment rules unchanged.
It exposes three starting tasks: review a document, research a question, and
continue a Matter. Matter navigation is Overview, Documents, Review, and Research.

## Review a document

Create or open a Matter, upload a supported file in Documents, and preview its
extracted text. Select Review document. Open Review to inspect the five-column
claim list. Selecting a claim opens the source/evidence panel; Escape closes it
and returns focus. Extra filters and metadata are available under Advanced.

Model assessments are proposed interpretations. Review notes and a reviewed flag
are stored separately. Editing a claim or replacing its finding resets that flag.
Save reviewer notes, investigate a claim, inspect its authority history, or export
an editable Markdown review report. Reports include case passages, source URLs,
coverage, unresolved citation counts, and reviewer notes. The existing CSV export
remains available.

## Research a question

The question form displays local corpus coverage and offers named court filters.
The default searches across courts in the local collection. A submitted question
opens a saved `/runs/{id}` page. Recent research links reopen the same run.

The page polls without a two-minute cutoff. Queued, running, stopping, failed,
completed, and partial results are distinct. It shows the current workflow stage
and last progress timestamp when available. Network errors retain the run and
retry polling. Deliberate retries reuse the saved run; unavailable persisted
content disables retry. Backend restart recovery marks unfinished runs interrupted.

The local research executor admits at most four active/queued jobs. Stop is
cooperative: queued jobs can be cancelled immediately; running jobs stop between
stages after the current operation returns. The workflow checks its 15-minute
budget between stages, not as an interruptible wall-clock guarantee. Existing
provider timeouts and bounded retries still apply to each provider call.

## Scope and limitations

Coverage describes the actual local index, not an assertion of complete or
current law. A missing result is not proof of legal irrelevance. The 14-case
sample remains an engineering fixture. This change does not establish model
semantic accuracy or full legal-research coverage.

Monitoring source-file paths and other operator controls are under an explicit
administrator disclosure. Run metadata excludes prompts and provider response
bodies. No credentials or generated corpora belong in Git.

## Validation

Use the standard backend pytest/Ruff/mypy/pre-commit and frontend
lint/typecheck/test/build checks. New regression coverage verifies long queued
runs, remount recovery, safe network errors, saved-run retries, bounded admission,
stop behavior, restart recovery, scope metadata, and reviewer-state invalidation.
On this Mac the generated editable-install file repeatedly reacquired UF_HIDDEN;
backend validation therefore uses an isolated Python 3.12 container without any
application workaround. Tests use fake providers; browser smoke tests use the
configured local Ollama runtime and existing sample corpus.

# M10 DOCX exports

Authenticated, editable Word exports use the existing single-operator bearer
boundary and durable matter/thread ownership checks. No files are persisted by
the export service, and no external fetch, model call or workflow resume occurs.

## Routes

- `GET /matters/{matter_id}/investigations/{thread_id}/export.docx`
- `GET /matters/{matter_id}/investigations/{thread_id}/deposition-plans/{plan_id}/export.docx?revision=2`

Send `Authorization: Bearer <operator token>` as a header. Successful responses
contain DOCX bytes with media type
`application/vnd.openxmlformats-officedocument.wordprocessingml.document`, an
attachment filename, `Cache-Control: no-store` and `nosniff`. The UI uses an
authenticated binary fetch and temporary Blob URL; credentials never enter URLs.

Investigation exports require an approved, signed M7 publication. Deposition
exports require an approved M9 revision whose signed M7 dependency still validates.
Omitting `revision` requires the current revision to be approved; it never silently
selects an older approval. An explicit revision (1–200) can export a historical
approval and labels its revision independently of any later draft.

Both exports validate the immutable journal digest, accepted evidence revisions,
source bytes and exact references under the existing publication fence. Working
artifacts are never substituted. Export does not reconcile approval intents or
change snapshots, plans or review history. Missing, tampered or stale dependencies
fail closed. Legitimate PARTIAL coverage and quarantined-candidate counts remain
visible; quarantined content is excluded.

## Content and limits

Investigation reports include metadata, recorded-material summary, reviewed
chronology/accounts, exact quotations, proposed findings, uncertainties and a
source appendix. Deposition reports include account-scoped dossiers and the exact
approved questions, rationale, selected citations and reviewer-approved order.
Hypotheses, disputed claims and suggested questions retain their uncertainty.
Empty reviewed sections are explicitly identified. M7 has no approval timestamp;
reports say “Not recorded” rather than inventing one. M9 approval metadata is used.

Reports use editable text, heading styles, consistent margins and page fields.
Unsafe XML/control characters are visibly escaped, never silently removed; normal
Unicode and source wording are preserved. Filenames are sanitized and bounded.
Rendering is deterministic in content for fixed inputs; ZIP metadata and Word/font
pagination may vary. No fonts or external templates are downloaded or embedded.

One export may render per server process; concurrent attempts return 503. Bounds:
2,000,000 emitted text characters, 10,000 paragraphs, 2,000 references and 8 MiB
DOCX output. Oversized requests fail safely instead of truncating evidence.

Errors use safe existing `detail` envelopes: 401 authentication, 403 operator
binding, 404 unavailable scope/revision, 409 publication/approval/source conflict,
422 invalid request and 503 busy/unavailable. Oversized exports return
409 `EXPORT_TOO_LARGE`. No private paths,
raw exception text or tokens are returned. The UI prevents duplicate downloads
and displays errors without automatically retrying or changing review state.

## Changed-file manifest

- `docs/product/ARCHITECTURE.md`
- `docs/product/DEPOSITION_API.md`
- `docs/product/DOCX_EXPORTS.md`
- `docs/product/INVESTIGATION_API.md`
- `frontend/components/DepositionWorkspace.test.tsx`
- `frontend/components/DepositionWorkspace.tsx`
- `frontend/components/DocxDownload.test.tsx`
- `frontend/components/DocxDownload.tsx`
- `frontend/components/InvestigationWorkspace.test.tsx`
- `frontend/components/InvestigationWorkspace.tsx`
- `frontend/lib/exports.test.ts`
- `frontend/lib/exports.ts`
- `pyproject.toml`
- `src/lextrace/api/app.py`
- `src/lextrace/api/exports.py`
- `src/lextrace/deposition/service.py`
- `src/lextrace/exports/__init__.py`
- `src/lextrace/exports/document.py`
- `src/lextrace/exports/render.py`
- `src/lextrace/exports/service.py`
- `tests/api/test_exports.py`
- `tests/exports_fixtures.py`
- `tests/integration/test_export_integrity.py`
- `tests/unit/test_docx_exports.py`

## Validation and remaining boundaries

Final targeted Python regression run: **329 passed, 0 failed**. Ruff check and
format check passed (11 Python files); mypy passed (11 source files);
`git diff --check` passed. Frontend checks ran once in the external clean test
copy: typecheck, ESLint and production build passed; Vitest **57 passed, 0 failed
across 11 files**. No frontend check stalled.

DOCX fixtures were rendered and inspected during implementation; further visual
polishing stopped at the operator's request. Word/font pagination remains client
dependent. Tests are offline, not live-provider evidence-quality validation. The
known M8 extraction defect is unchanged; M11 comprehensive smoke testing remains
separate. No commit or push was performed.

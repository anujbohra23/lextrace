# M9 witness and deposition preparation

M9 uses current, human-signed M7 investigative snapshots. It creates separate
human-reviewed preparation revisions; it never changes the M7 snapshot and does
not export DOCX. All generation is bounded and deterministic, with **zero model
calls**, searches, external acquisition, or agent execution.

## Grounding and identity semantics

`InvestigationWorkflow.approved_snapshot(thread_id)` is the authoritative current
publication read. M9 loads the immutable journal revision identified by its
`artifact_digest`, not the working artifact cache or latest journal entry. It
checks the signed source/review/participant-link dependencies, accepted M4
revisions, original candidate identity, immutable bytes, and exact M3 document /
version / section / Python code-point spans before using evidence.

An attributable accepted assertion produces an **account-scoped witness dossier**.
Identical or similar attribution strings do not merge dossiers. M7 `HumanEntityLink`
associates accounts with subject/participant mentions; it does **not** establish
speaker identity. M9 displays those reviewed associations as participant context,
retains identity gaps, and never promotes them into a speaker merge or inferred
personal knowledge. Account timing preserves the original and reviewer date
interpretations; related event chronology is context, not witness testimony.

Question categories are FOUNDATION, TIMELINE, CLARIFICATION,
POTENTIAL_INCONSISTENCY, and FOLLOW_UP. Foundation does not invent a biography.
Timeline questions clarify stated timing without inventing dates. Confrontation
and reconciliation questions require two distinct source documents with known
attribution, accepted source accounts matching the M7 pair, a signed finding with
`INDEPENDENT_STRUCTURED_SUPPORT` and `POTENTIAL_INCONSISTENCY`, and citations from
both accounts. Findings remain **unverified investigative hypotheses**, not proven
contradictions, dishonesty, liability or credibility assessments.

The basis outcome describes the generated preparation scope:

- `FULLY_SOURCED`: generated questions have reviewed, exactly traceable sources,
  with no detected coverage/exclusion limitations. This is not a completeness or
  truth certification.
- `LIMITED`: partial/OCR coverage, quarantined candidates, coverage diagnostics,
  unattributed exclusions, unsupported findings, investigation limitations, or a
  generation budget constrain preparation. Approval requires explicit acknowledgement.
- `BLOCKED`: no attributable reviewed account supports questions. It contains no
  invented witnesses or filler questions and cannot be approved. An unpublished,
  stale, canceled or missing M7 snapshot instead fails the publication prerequisite
  with a safe 404/409; approving empty M8 extraction is never bypassed.

Only valid accepted evidence enters the dossier/registry. Quarantined material is
reported as a limitation, never affirmative question content. The known M8 live
extraction defect (PARTIAL / CANDIDATES_QUARANTINED / empty verified chronology)
remains M11; M9 does not repair or fill that gap.

## Routes

Base: `/matters/{matter_id}/investigations/{thread_id}/deposition-plans`.
The existing M8 bearer middleware and persistent single-operator workspace binding
protect every route. Responses use `Cache-Control: no-store`. Existing-run
ownership and the current M7 snapshot are checked before every operation, including
history and approved-revision reads. Source replacement, deletion, review changes,
or file damage invalidate access. A plan ID alone never authorizes access.

| Method | Suffix | Response |
| --- | --- | --- |
| POST | empty | 201 `PlanView`; create or recover the request's existing plan |
| GET | empty | 200 array of plan summaries for the current signed snapshot |
| GET | `/{plan_id}` | 200 `PlanView` with immutable basis and authoritative current revision |
| GET | `/{plan_id}/witnesses` | 200 array of source-account dossiers |
| POST | `/{plan_id}/review` | 200 `PlanView` for the recorded decision outcome |
| GET | `/{plan_id}/history` | 200 append-only revision array, including server reviewer/time |
| GET | `/{plan_id}/approved/{revision}` | 200 exact approved revision with only INCLUDE questions in `current.questions` |

Create example (IDs abbreviated; snapshot IDs are 64 lowercase hexadecimal digits):

```json
{"snapshot_id":"<current M7 snapshot ID>","request_id":"stable-client-request-id"}
```

Use the exact `snapshot_id` returned by the investigation. Retain `request_id`
across an uncertain response. Identical retries recover the existing plan; they do
not generate duplicate rows. Source checks are still required on every retry.

`PlanView` has `basis` and `current`. The basis includes plan/matter/thread/snapshot
IDs, signed source/review/artifact dependency identifiers, coverage, outcome,
limitations, witness dossiers, an immutable evidence registry, generated question
candidates and per-question citation policy. Registry IDs are SHA-256 identities
of complete immutable M3 references, including verbatim quotes. `current` contains
revision, status, question drafts, action/decision ID, server-derived reviewer ID,
server UTC timestamp and limitation acknowledgement. The basis candidates are
**draft context**, not an approval of excluded questions.

Review example (copy all questions from the current revision):

```json
{
  "review": {
    "decision_id":"stable-review-command-id",
    "expected_revision":1,
    "action":"APPROVE",
    "confirm":true,
    "acknowledge_limitations":true,
    "questions":[{
      "question_id":"<server question hash>",
      "witness_id":"<server account-scoped witness hash>",
      "category":"FOUNDATION",
      "wording":"Could you explain the basis of the quoted account?",
      "rationale":"Clarify the limits of the attributed source.",
      "evidence_ids":["<server evidence hash>"],
      "order":0,
      "group":"Foundation interview",
      "disposition":"INCLUDE"
    }]
  }
}
```

Actions: `REVISE` creates a DRAFT revision, `APPROVE` creates an APPROVED revision,
`REJECT` creates REJECTED, and `DEFER` creates DEFERRED. Every action appends a new
revision atomically. Approving requires explicit confirmation, at least one INCLUDE
question, sufficient evidence, and explicit acknowledgement for LIMITED bases.
Question dispositions INCLUDE/REJECT/DEFER allow excluding individual questions.
Excluded questions remain audit material, never approved question content.

Wording/rationale editing is bounded to 2,000 characters, wording must end with
`?`, and order values (0–1000) must be unique. Optional preparation groups are
editorial labels of at most 120 characters, never identity merges. Question IDs,
witness identities and categories cannot be replaced. Evidence selections must
remain within that question's original reviewed accounts, with at least one exact
reference from **each** required account. Nonexistent, unrelated, stale or
cross-matter references cannot be added through a browser. Every original question
must remain in the revision; exclude via disposition instead of deleting its ID.

Free wording/rationale edits are explicitly human-authored interpretations.
Deterministic reference checks establish provenance and allowed account scope, not
semantic entailment of arbitrary edited language. The human approver must check
premises against the displayed quotations. No automated truth or semantic-support
certification is claimed for edits.

Do not send reviewer identity or timestamps in review JSON. `require_operator`
supplies the reviewer; the server supplies UTC timestamps. Identical ordered
request/reviewer/decision ID retries return the original recorded revision;
conflicting reuse or stale `expected_revision` returns 409. A subsequent GET is
the authoritative current state, which may be newer than a retry's saved outcome.
Any edit after approval creates a new DRAFT requiring fresh approval. Historical
approved revisions remain immutable and explicitly addressable, but source
currency is still required and later revisions supersede them as current material.

## Persistence, bounds and recovery

Additive investigation migration 5 creates `deposition_plans`,
`deposition_revisions`, and `deposition_decisions` in the existing private matter
SQLite database. No new workflow/checkpointer or datastore is introduced. Basis and
revision payloads are hash-checked. A single `BEGIN IMMEDIATE` fences source/review
writers while M9 rechecks signed dependencies and commits each create/review
operation. Revision and idempotency journal writes commit together; crash before
commit rolls back both, and retry after commit reads the same decision. M4 public
reads occur before that write transaction; exact signed dependency checks inside
the transaction fence intervening changes. The existing M7 publication is read
through its supported current-snapshot boundary first.

The basis is deterministic and revalidated against the signed journal on use.
Bounds: 100 dossiers, 200 generated questions, 20 plans per investigation, 200
revisions per plan, and a 32 MiB per-payload serialization limit. Generation caps
are explicitly LIMITED; persistence/review caps fail closed with a safe conflict.
Plan listing returns bounded summaries. Deleting a source cascades through M7 run
and snapshot ownership to all M9 plans, revisions and decisions. No private plan
text is retained as an orphan history after source deletion.

The Next.js workspace is linked from each investigation at
`/matters/{matter_id}/investigations/{thread_id}/deposition` and has dedicated plan
URLs. Refresh/reopening reads server state. Unsaved wording is not silently
replaced when a newer revision is discovered; the user explicitly reloads the
server draft. Submitted create/review payloads are retained byte-for-byte in
per-tab session storage for safe retries. Failed/conflicting submissions refresh
server state without submitting a changed request. Discarding a local retry does
not undo a committed server decision. Closing the tab loses unacknowledged local
retry data; durable plans remain available in the investigation's plan list.

Errors use `{"detail":"CODE"}` and never include raw inputs, tokens, paths,
checkpoint internals, provider failures or traces. 401/403 are unchanged M8 auth
errors; 404 denotes missing/scoped plan/approval, 409 stale snapshot/source/revision,
conflicting retry or exhausted bounds, 422 malformed review/evidence/confirmation,
and 503 unavailable runtime/storage. `DEPOSITION_*` codes distinguish these domain
errors. No export endpoint or DOCX action is included; export remains M10.

## M9 delivery and validation

Files added or changed for M9 (23 total):

```text
docs/product/ARCHITECTURE.md
docs/product/INVESTIGATION_API.md
docs/product/DEPOSITION_API.md
src/lextrace/api/app.py
src/lextrace/api/deposition.py
src/lextrace/deposition/__init__.py
src/lextrace/deposition/contracts.py
src/lextrace/deposition/generation.py
src/lextrace/deposition/service.py
src/lextrace/deposition/storage.py
src/lextrace/investigations/migrations.py
frontend/app/matters/[id]/investigations/[threadId]/deposition/page.tsx
frontend/app/matters/[id]/investigations/[threadId]/deposition/[planId]/page.tsx
frontend/components/InvestigationWorkspace.tsx
frontend/components/DepositionWorkspace.tsx
frontend/components/DepositionWorkspace.test.tsx
frontend/lib/deposition.ts
tests/deposition_fixtures.py
tests/unit/test_deposition.py
tests/unit/test_investigation_repository.py
tests/api/test_deposition_api.py
tests/integration/test_deposition_recovery.py
tests/integration/test_document_delete.py
```

Offline validation: **291 Python tests passed, 0 failed** across M9, all API tests,
M4/M7/M8 repository/review/durability/workflow, upload, and deletion regressions.
Targeted Ruff lint/format and strict mypy passed on all 14 changed Python files;
`git diff --check` passed. The legacy deletion API test's synthetic settings were
updated to authenticate under the existing M8 boundary; production auth behavior
was not changed.

The frontend sources were copied with rsync (excluding dependencies/build/git)
into the existing clean `~/Developer/lextrace-frontend-test` directory. Typecheck,
ESLint, **45 Vitest tests across 9 files (0 failed)**, and the Next.js production
build each passed on their single run there, including both new deposition routes.
No dependency was added and no test-copy source change was copied back.

No live-provider or real-case deposition-quality validation was performed. The
M8 extraction defect remains unresolved and outside M9. Human-edited wording
still requires semantic judgment by the approver; exact source provenance is not
a truth certificate. Account-scoped dossiers deliberately retain unresolved
speaker identity rather than guessing a merge. No DOCX export, M11 audit, commit
or push is included in this delivery.

## M10 authenticated Word exports

See [DOCX_EXPORTS.md](DOCX_EXPORTS.md) for investigation and approved deposition
export routes, trust checks, revision selection, resource limits and UI behavior.
Exports preserve M7/M9 approval boundaries and do not resume workflows.

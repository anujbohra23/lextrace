# M8A investigation API

All routes require the M8-Auth bearer credential and bound operator workspace
(see [single-operator authentication](SINGLE_OPERATOR_AUTH.md)). Responses use
`Cache-Control: no-store`. Reviewer identity is derived from the authenticated
operator; request bodies cannot contain `reviewer_id`, including nested evidence
commands. No frontend changes are required.

Base path: `/matters/{matter_id}/investigations`.

| Method | Suffix | Contract |
| --- | --- | --- |
| POST | empty | Start a durable investigation; 201 investigation view. |
| GET | empty | List matter investigations in durable creation order; 200 array of views. |
| GET | `/eligible-versions` | List eligible active document/version pairs for source selection; 200 array. |
| GET | `/{thread_id}` | Reconcile authorized work and read current status/review; 200 view. |
| POST | `/{thread_id}/decisions` | Submit evidence/findings review decision; 200 current view. |
| GET | `/{thread_id}/snapshots/{snapshot_id}` | Read current M7 immutable signed snapshot by exact ID. |
| GET | `/{thread_id}/snapshots/{snapshot_id}/artifacts/{artifact_id}` | Read the snapshot's reviewed chronology or individual finding by exact ID. |

Every existing-run operation checks the durable matter/thread association before
accessing checkpoints or artifacts. Run IDs alone never authorize access. There
is no separate resume endpoint: decisions use M7 `resume`, while reads use M7
`reopen` to reconcile already-authorized pending intents and checkpoint gaps.
These synchronous operations return after reaching a review gate or terminal
state. Retrying a start creates a new investigation.

Before starting, fetch `GET /matters/{matter_id}/investigations/eligible-versions`.
It returns only the current `document_id`/`version_id` pairs, ordered by document
ID, for live matter-owned documents whose ingestion status is `READY` and whose
registered manifest is active and non-retired:

```json
[{"document_id":"document-id","version_id":"version-id"}]
```

There is **no persisted document approval status**. Eligibility means that a
source can be selected by the operator, not that its contents are human-reviewed
or true. The existing M7 name `ApprovedVersion` and POST field `approved_versions`
represent the operator's explicitly selected source scope; they do not add an
approval model. READY sources with partial extraction coverage can be selected;
coverage acknowledgement remains required at review gates. OCR-required sources,
deleted documents, inactive/retired versions, and legacy documents without
registered active manifests are excluded. Listing does not repair/register
manifests, inspect private source bytes, run a model, or create a workflow.

An empty matter returns `[]`; a missing matter returns 404
`INVESTIGATION_NOT_FOUND`. This endpoint uses the existing operator authentication
and workspace binding and does not require an LLM model to be configured. Its
response is a point-in-time selection aid. M7 still validates the exact submitted
pairs and input currency when starting; versions retired or replaced after a
list read produce a conflict. Refresh eligibility and explicitly select again;
never automatically retry starting a new run.

Start request (copy the selected pairs from that response):

```json
{"approved_versions":[{"document_id":"document-id","version_id":"version-id"}]}
```

An optional `budgets` object uses the existing M7 `WorkflowBudgets` schema and
defaults. No documents, source bytes, or arbitrary graph state are accepted.

Investigation view example (IDs and digest abbreviated for readability):

```json
{
  "matter_id":"matter-id","thread_id":"thread-id","run_id":"run-id",
  "stage":"WAITING_REVIEW","errors":[],"coverage":["COMPLETE"],
  "checkpoint":"evidence","token":"review-token","digest":"approval-digest",
  "review":{"records":[],"chronology":null,"findings":null},
  "snapshot_id":null
}
```

`stage` is `RUNNING`, `WAITING_REVIEW`, `BLOCKED`, `CANCELED`, or `APPROVED`.
`checkpoint` exposes only the active interrupt name (`evidence` or `findings`),
never raw LangGraph checkpoints. Token and digest are present only at an active
interrupt. Copy them unchanged into the decision. `review.records` contains M4
review revisions with original evidence references; `chronology` contains the M5
projection (reviewed at the findings gate); `findings` contains the M6 result at
the findings gate. These are private review materials, not signed findings.
Terminal views have null review, checkpoint, token and digest. An approved view
includes `snapshot_id`.

Decision request:

```json
{
  "run_id":"run-id","gate":"evidence","token":"review-token",
  "digest":"<64 lowercase hex characters>","decision":"APPROVE",
  "acknowledge_incomplete_coverage":false,
  "entity_links":[],"evidence_decisions":[]
}
```

Use `REJECT` to terminate at either gate (`BLOCKED`, error `REJECTED`). Approve
both gates to publish. Incomplete coverage requires explicit acknowledgement at
each approval gate. Evidence edits and entity links are accepted only at the
evidence gate under existing M7/M4 scope and optimistic revision rules.

Each optional `entity_links` item is an M7 `HumanEntityLink` (`account` review
target and `mentions` array). Each optional evidence decision uses the M4 fields
`decision_id`, UTC `timestamp`, `target`, `expected_revision`, `decision`
(`ACCEPT`, `REJECT`, `DEFER`, `EDIT`), and optional `explanation`,
`date_correction`, `resolved_entity_id`. Identity is added by the server.
Preserve the exact ordered request, including command IDs and timestamps, when
retrying. Identical decisions reuse the M7 durable intent; conflicting reuse
returns 409. A retry returns the run's current view, which may have advanced
since the original decision.

Snapshot responses are the unchanged M7 `ApprovedInvestigationSnapshot` schema:
`snapshot_id`, matter/run/thread identities, source and review dependencies and
digests, approval digests, `chronology_id`, `finding_ids`, reviewer ID, and meaning
`HUMAN_SIGNED_INVESTIGATION_HYPOTHESES`. Use `chronology_id` or a `finding_ids`
entry for artifact reads. Stale or unavailable published inputs fail closed;
historical snapshots are not represented as current. Artifact reads expose only
these signed identifiers, never the private artifact journal or filesystem.

Errors have the envelope `{"detail":"CODE"}`:

| Status | Code |
| --- | --- |
| 401/403/503 | Existing M8-Auth codes; authentication/workspace binding failed. |
| 404 | `INVESTIGATION_NOT_FOUND`: missing matter/thread or wrong matter/thread association. |
| 404 | `INVESTIGATION_ARTIFACT_NOT_FOUND`: identifier is absent from this snapshot. |
| 409 | `INVESTIGATION_STATE_CONFLICT`: invalid/stale scope, review state, conflicting retry, unavailable checkpoint, or busy durable runtime. |
| 422 | `INVESTIGATION_INVALID_REQUEST`: malformed request or forbidden fields. |
| 503 | `INVESTIGATION_UNAVAILABLE`: provider/storage/runtime unavailable. |
| 503 | `INVESTIGATION_DURABILITY_REQUIRED`: injected runtime does not support durability. |

Recovery uses the existing matter database and private M7 SQLite checkpoints.
Reading the run after server restart reconciles pending approval intents without
resubmitting them. Retrying the original decision is also safe. M7's immutable
publication transaction prevents duplicate snapshots. Unknown external tool
outcomes fail closed under the existing M7 recovery contract.

## Frontend workspace (M8B)

Open **Investigations** from a matter, then follow a run link to its dedicated
review page. Refresh reads the durable server state; approval confirmation and
incomplete-coverage acknowledgement are required anew at each gate. Published
views fetch the snapshot and signed artifact endpoints rather than reusing draft
review contents.

An unresolved submitted decision is kept in this browser tab's session storage,
including its original ordered commands, IDs, UTC timestamps, and approval token.
Retry sends that exact payload. A conflict refreshes the server view without
submitting another decision. Explicitly discarding a saved request does not undo
server work. Closing the tab loses this local retry envelope, but existing runs
remain available through the matter investigation list.

The start page reads `/eligible-versions` alongside the investigation list and
offers only those exact server-provided pairs. Empty or unavailable eligibility
keeps starting disabled. A start conflict clears the old selection and refreshes
the eligible list without resubmitting. The frontend never derives version IDs
from document content, readiness alone, or old snapshots.

## M9 deposition preparation

Current approved investigations link to an account-scoped witness and deposition
workspace. The authenticated deposition endpoints consume the exact signed M7
artifact revision and do not modify investigation approval or publication.
See [M9 deposition contracts](DEPOSITION_API.md) for create/read/review/history and
approved-plan routes, evidence constraints, partial/blocked outcomes and recovery.

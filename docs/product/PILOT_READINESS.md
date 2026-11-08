# M11 pilot readiness — FAIL (acceptance blocked)

## Remediation continuation — current assessment (2026-11-08)

**FAIL / not pilot-ready.** The unexplained assertion is localized and the
harness now respects the human-review boundary. Durable start identity is
implemented and regression-tested. The single post-fix live smoke stopped safely
because the model did not supply two sufficiently grounded speaker accounts;
findings, publication and exports were **not** reached in that attempt. No
approval was manufactured to turn this into a passing test. The initial M11 audit
below is retained as historical evidence; this continuation supersedes its open
status for M11-06 and its unlocalized-assertion diagnosis for M11-08.

### Preserved baseline and focused changes

Continuation began on `feat/investigations-v1` with the prior M11 changes still
uncommitted. All were preserved. No broad audit, visual polish, dependency change,
real-matter mutation, commit or push was performed. Changes were confined to the
smoke/retry paths, their tests, start UI integration and contracts.

### Blocker 1: exact failed invariant and safe remediation

One bounded diagnostic replay genuinely used local Ollama `qwen3:8b`. It located
**source_invariants, pre-fix script line 197**, after three calls (45-second request timeout, zero retries, 2,000 completion
and 8,192 context tokens). The failed condition was:

```python
account.attributed_to + " states:" in account.evidence_references[0].exact_quote
```

It was not an inconsistency verifier, DOCX renderer or checkpoint assertion. The
harness treated every provenance-valid *proposed* attribution as an already
reviewed witness account. Exact source provenance does not establish that an
actor mentioned in a statement is its speaker. The safe replay output retained
counts/line, not the discarded model quotation/label; no specific discarded label
is invented here.

A regression adds an exact, source-valid action quotation about Jordan to Alex's
statement and proposes Jordan as speaker. It failed at the same old attribution
assertion before the fix. The corrected TEST-ONLY reviewer accepts only accounts
with the fixture's explicit correct named attribution and substantive statement,
and rejects other attribution proposals. Existing M7 semantics deliberately block
publication if any run input is rejected; that contract was **not changed** to
make the smoke pass. A second regression removes a supported speaker account and
proves that no approval intent or snapshot is written.

Production prompt guidance now explicitly distinguishes an actor from a named
speaker. Verification still rejects inexact/ambiguous quotes and unsupported
structure. Stage/line/call diagnostics remain text-free. This is a harness/review
assumption fix, **not certification of model attribution quality**.

**Single post-fix live command:**

```sh
python scripts/pilot_smoke.py --live --with-findings
```

Configuration: `qwen3:8b`, temperature zero, 16,384 context tokens, 8,192 maximum
completion tokens, 90-second request timeout, zero retries; at most three
extraction calls plus two reasoning calls, one pair and one search. The larger
bounded response/context allowance avoids using the former 2,000-token smoke
cap for the long M6 structured schema. These harness allowances do not change
production provider defaults. Actual calls: **3**, then exit **2 / BLOCKED**.

Observed final live result:

- Health 200, anonymous 401, three READY uploads and three eligible versions.
- Start 201; evidence checkpoint; COMPLETE geometric coverage for all three TXT
  sources. Foreign-matter run read 404.
- Six verified candidates: two assertions, two events, two temporal expressions;
  zero entity mentions. One proposal remained quarantined with
  `UNSOURCED_ASSERTION_STRUCTURE`.
- One proposed attribution failed the explicit fixture review requirements.
  Result: `BLOCKED: insufficient attributable accounts/events`.
- No human approvals, findings calls, signed publication, M9 approval or DOCX
  downloads were attempted after that guard. No post-fix provider retry followed.

Thus the prior *unlocalized* assertion is resolved, but live extraction/attribution
and complete findings acceptance remain a release blocker (M11-09). Earlier
successful export/restart smoke evidence in the historical report must not be
substituted for this failed final live acceptance.

### Blocker 2: durable server start identity

`POST /matters/{matter_id}/investigations` now accepts optional `request_id`.
See [the start contract](INVESTIGATION_API.md#durable-start-request-identity-m11-remediation).
The same authenticated operator/matter/key and normalized ordered source/budget
payload returns the same run's **current** view (201). Different keys intentionally
create distinct runs; omitting the key preserves legacy non-idempotent behavior.
Keys are never inferred from equal source sets.

Additive SQLite migration **6** binds request identity, input digest and initial
run seed atomically with run/initial-journal creation. Existing M7 locks and
recovery are reused. Every retry checks active owned sources, operator identity,
matter/thread/run association, original seed integrity and current source bytes.
A conflicting payload or temporarily busy peer returns safe 409. The caller must
retry the same saved key/payload; no second provider invocation is scheduled.

Crash before the transaction rolls back both request and run; crash after commit
but before the first graph checkpoint resumes the same initial run. Existing-node
recovery uses existing M7 journals/intents. Missing checkpoints for an *executed*
run fail closed rather than restarting extraction. Deletion clears the seed and
thread pointer but retains an unavailable-key tombstone until matter deletion.
This prevents stale request resurrection without retaining deleted source state.

Tests exercise repeated/concurrent requests, separate workflow-instance busy/retry,
restart at both sides of request commit and after extraction commit, current-state
read-back after review, distinct and legacy starts, changed payloads, malformed
keys, wrong operator/matter/thread seed, stale versions, damaged source bytes,
missing executed checkpoints and deletion cleanup. The separate-instance test
uses the real OS advisory fence; no claim of a new distributed lock is made.

The existing UI now stores the request ID and byte-identical payload before POST,
keeps new starts locked during uncertain acknowledgement, and supports explicit
identical retry across refresh/remount. No bearer token is stored in this new
envelope. Closing the tab can lose the local envelope; server runs remain listed.
Old clients that omit the optional key remain non-idempotent by design.

### Final validation for this continuation

Counts are separate, overlapping suites and are not summed.

```sh
pytest tests/api/test_investigation_start_identity.py tests/integration/test_start_request_recovery.py tests/integration/test_pilot_smoke.py -q --tb=short
# 23 passed, 0 failed (3.23 seconds).
pytest -q --tb=short
# 968 passed, 0 failed (39.18 seconds).
ruff check src tests scripts/pilot_smoke.py
ruff format --check src tests scripts/pilot_smoke.py
mypy src tests scripts/pilot_smoke.py
git diff --check
```

The first start-identity red run produced **3 failed, 1 passed** before support was
implemented. The source-actor attribution regression failed before its harness
fix. Final Ruff lint and format checks passed (180 files), strict mypy passed
(180 source files), and `git diff --check` passed. No check was skipped or hung.

Frontend sources were synced with:

```sh
rsync -a --exclude=node_modules --exclude=.next --exclude=.git --exclude='*.tsbuildinfo' frontend/ ~/Developer/lextrace-frontend-test/
```

Executed once in that existing external checkout, with no edits to the test copy:

```sh
npm run typecheck && npm run lint && npm test -- --reporter=dot && npm run build
```

All passed: **58 Vitest tests, 0 failed, 11 files**, TypeScript, ESLint and production
build. No tool hung. The new browser test proves a lost start acknowledgement is
retried with the same ID and exact body after remount; existing review retries and
authentication checks remain passing.

### Remaining acceptance / operator actions

- [x] Locate the exact old invariant and distinguish proposed attribution from
  reviewed source accounts; preserve strict provenance and fail-closed review.
- [x] Durable opt-in start identity, safe retry/restart/concurrency and UI adoption.
- [x] Focused regressions and final automated quality gates.
- [ ] Obtain sufficiently grounded accounts from a bounded supervised live
  extraction. Inspect/review role attribution; never accept an actor as speaker
  merely because their name occurs in a quote.
- [ ] Complete live inconsistency search/independent assessment and the downstream
  review/publication/M9/DOCX chain on that valid evidence. This continuation's
  final live attempt did **not** reach those stages.
- [ ] Complete manual live-browser acceptance and deployment/backup procedures
  from the historical checklist. No real-data approval is authorized by this report.

Code is ready for review subject to the recorded green gates; **pilot release is
not ready**. Migration 6 is additive; back up the private workspace before normal
upgrade. No existing signed snapshot or approval is rewritten.

### Exact files touched in this continuation (14)

- `docs/product/PILOT_READINESS.md`
- `docs/product/INVESTIGATION_API.md`
- `scripts/pilot_smoke.py`
- `frontend/components/InvestigationWorkspace.tsx`
- `frontend/components/InvestigationWorkspace.test.tsx`
- `src/lextrace/api/investigations.py`
- `src/lextrace/investigations/durable.py`
- `src/lextrace/investigations/factual_extraction.py`
- `src/lextrace/investigations/migrations.py`
- `src/lextrace/investigations/workflow.py`
- `tests/api/test_investigation_start_identity.py`
- `tests/integration/test_pilot_smoke.py`
- `tests/integration/test_start_request_recovery.py`
- `tests/unit/test_investigation_repository.py`

## Historical initial M11 assessment (retained unchanged below)

## Executive assessment

An earlier post-fix Warehouse Delivery Dispute run completed a **genuine local Ollama
extraction → TEST-ONLY evidence review → findings gate → signed publication → M9
review → both editable DOCX exports → restart/read-back** chain. Exact-source
checks were not relaxed. The pre-fix live run reproduced the empty-evidence
failure; the successful quote-anchor run verified 13 records, including two separately
attributed accounts and three events, with zero quarantined proposals.

**Pilot acceptance is FAIL, not PASS.** After the successful export/restart
smoke, a capped nonzero-search attempt produced zero candidate pairs. Inspection
confirmed the extractor never populated the descriptor required by M6. The final
source-verbatim subject/predicate/object adapter fixes that omission and passes
unit/integration tests, but the last extended live attempt failed a harness
invariant. Its safe terminal output retained only AssertionError, so the exact
failure stage/cause is **not established**. No further provider attempts were
made. That live acceptance blocker must be investigated before a pilot.

Browser interaction was covered by offline component tests, not a live interactive
UI session. Duplicate investigation starts remain non-idempotent. No security
bypass was observed, but this does not resolve the live reasoning acceptance gap.
Complete the operator checklist below before using real matters.

No real matter was approved, edited, deleted or replayed. No commit or push was
performed. This report contains no tokens, private paths or real matter contents.

## Baseline and environment

- Branch: `feat/investigations-v1`; clean baseline `35a5d2a` (documentation commit
  after M10 `6331b53`). There were **no pre-existing uncommitted changes**, including
  the three product architecture/API documents mentioned in the request. Those
  documents were left unchanged in M11.
- Darwin 24.6.0, arm64; Python 3.12.12; Node 24.8.0; npm 11.6.0.
- FastAPI 0.162.0, Pydantic 2.14.3, HTTPX 0.28.1, pytest 9.2.0,
  Ruff 0.11.13, mypy 1.20.2.
- LangGraph 1.2.20, langgraph-checkpoint 4.3.1,
  langgraph-checkpoint-sqlite 3.1.3; python-docx 1.2.0.
- Frontend: Next.js 16.3.4, React 19.3.0, Vitest 3.2.7. All frontend checks ran
  once in the existing external validation checkout after rsync; no test-copy
  edits were made or copied back. No frontend source changes were needed.
- **Live provider actually used:** local Ollama 0.18.2, `qwen3:8b`, 8.2B Q4_K_M,
  model digest `500a1f067a9f782620b40bee6f7b0c89e17ae61f686b92c24933e4ca4b2b8b41`.
  Model availability and server version were queried, not assumed. Sandbox-local
  networking initially failed; the authorized host-network probe succeeded.
- Each live run allowed three extraction requests, 45 seconds per request, zero
  provider retries, 2,000 completion tokens, 8,192 context tokens and temperature
  zero. Workflow budget: 10 model calls, three extraction windows; early runs used
  zero searches. Extended attempts permitted one search, one pair and at most two
  additional model calls. Five controlled runs were made: original reproduction,
  diagnostic comparison, successful quote-schema smoke, zero-pair extended probe,
  final source-structure extended probe. Four completed runs each used three
  extraction calls; the final attempt was bounded to at most five total calls,
  but its actual count was not retained. There were no unbounded calls or polling.
  The final production schema includes source structure fields added after the
  earlier successful live smoke; its full live acceptance is not claimed.

## Real entry points and isolated data flow

`api.app.create_app` serves health, matter creation/upload, the M8 investigation
router, M9 deposition router and M10 export router. The harness uses FastAPI
TestClient with the real routing/middleware, TXT parser, source store, repository,
SQLite checkpointer, extraction/verification, workflow, review persistence,
publication, M9 generator and DOCX renderer. Only the provider is substituted in
**offline** tests; the live run uses `OllamaLLM` directly.

Every run creates temporary matter/runtime/cache database paths and a private
matter root. M7's checkpoint database lives beneath that temporary private root.
The runtime/cache paths are isolated even when these services do not need to open
them. Two independent fictional matters are created. The synthetic source files
live in `scripts/pilot_smoke.py`, outside production artifact stores. Temporary
files are removed after the run. The harness never reads environment credentials
or existing matter stores; it generates its own ephemeral operator token.

Authoritative boundaries remain:

- `MatterStore.require_investigation_matter` and the M8 operator/workspace binding.
- `InvestigationWorkflow.approved_snapshot`, signed source/review dependencies,
  and `DurableRuntime.load(..., digest=snapshot.artifact_digest)`.
- M9 `DepositionService.published` and exact `approved_in_context` revisions.
- `GET /matters/{matter_id}/investigations/{thread_id}/export.docx` and
  `GET /matters/{matter_id}/investigations/{thread_id}/deposition-plans/{plan_id}/export.docx?revision=N`.

Production configuration names, **without credential values**:
`LEXTRACE_OPERATOR_ID`, `LEXTRACE_OPERATOR_API_SECRET`, `LEXTRACE_MATTER_DB`,
`LEXTRACE_PRIVATE_MATTER_ROOT`, `LEXTRACE_RUNTIME_DB`, `LEXTRACE_CACHE_DB`,
`LEXTRACE_LLM_PROVIDER`, `LEXTRACE_LLM_MODEL`, `OLLAMA_BASE_URL`,
`LEXTRACE_LLM_TIMEOUT`. Legacy adoption is an explicit operator action via
`LEXTRACE_ADOPT_LEGACY_MATTERS`; the smoke does not need it. Public legal retrieval
requires a compatible configured index; external CourtListener acquisition is not
part of this smoke and was not invoked.

## Stage matrix

PASS means the named observation was actually checked, not general product quality.

| Stage | Result | Evidence and boundary |
| --- | --- | --- |
| Health/auth | PASS | Health 200; anonymous matter access 401; authorized requests succeed |
| Matter isolation | PASS | Two fictional matters; foreign thread/read/export returns 404 |
| TXT upload/READY | PASS | Three actual multipart uploads and immutable manifests |
| Eligible versions | PASS | Three exact server document/version pairs used; no invented IDs |
| Start | PASS / limitation | Live smoke starts once; separate offline duplicate probe creates two distinct runs |
| Live factual extraction | PASS earlier / final acceptance incomplete | Quote-anchor live run: 13 verified records, zero quarantines; final extended terminal lacked stage counters |
| Candidate provenance | PASS | Full text slices equal every quote; version/section checks run independently |
| Coverage | PASS for geometry | Three COMPLETE text-processing manifests; this does not certify extraction recall |
| Separate accounts | PASS | Two named source accounts remain separate dossiers; no speaker identity merge |
| Reviewed chronology | PASS | Three events; one same-day group after explicit TEST-ONLY date corrections |
| Evidence gate/retry | PASS | Exact current token/digest/run; ordered EDIT then ACCEPT commands; identical retry stable |
| Live inconsistency search/support | FAIL / acceptance blocked | Nonzero-search probe found no pairs; source-structure adapter fixed and tested offline, but final live extended attempt failed an invariant |
| Findings gate | PASS earlier for state machine | TEST-ONLY approval of recorded empty/bounded result; no live independently supported finding is certified |
| Signed publication/artifacts | PASS | Snapshot and immutable chronology read after both gates; unsigned export denied |
| M9 generation | PASS earlier, limited scope | Two separate dossiers, four grounded questions; no invented inconsistency questions |
| M9 editing/reordering/review | PASS | Human wording/group/order edits, explicit approval, identical retry and stale conflict |
| Investigation DOCX | PASS | Authenticated bytes loaded with python-docx; editable paragraphs, quotes and source IDs |
| Deposition DOCX | PASS | Exact approved revision, questions/citations; draft export denied |
| Export immutability | PASS | Offline smoke compares entire domain/checkpoint SQL dumps before/after downloads |
| Restart | PASS | Fresh workflow/app instance reads same signed snapshot, approved plan and DOCX |
| Live browser workflow | BLOCKED / untested | Vitest/typecheck/lint/build passed; no interactive browser acceptance claim |
| M1–M6 regressions | PASS offline | Wider Python suite includes retrieval, authority, graph, matter and research tests |

Date corrections interpret the explicitly quoted March 5, 2025 calendar date only.
The chronology does not claim resolved clock-time ordering between contradictory
accounts. All approvals are labeled TEST-ONLY and confined to fictional sources.

## Defect register

| ID / severity | Reproduction and root cause | Fix / regression | Remaining risk |
| --- | --- | --- | --- |
| M11-01 / medium | TXT sectioning preserves source bytes but trims paragraph separators; extraction treated leading/trailing/newline whitespace as NO_TRUSTED_SECTION | Deterministically inspect whitespace-only gaps as processed spans, without a model call. `test_txt_whitespace_separators_are_inspected_not_missing` failed before the fix; substantive-gap regression remains PARTIAL | COMPLETE is text-processing coverage, not semantic completeness; non-whitespace omissions still fail coverage |
| M11-02 / high | Live original run: 26 proposed candidates, all quarantined. Diagnostic run exposed quote/offset mismatches and contradictory kind metadata; local model character counting did not match exact source spans | Production extraction uses a kind-specific quote-only schema. Server derives offsets only for a unique exact match in the current trusted section window. Ambiguous/nonexistent quotes are quarantined; malformed supplied offsets are never repaired. Legacy offset adapter stays strict. Tests cover ambiguity, unsourced speaker, invalid offsets/metadata and Unicode | Local models can still omit facts or emit invalid quotes; strict rejection remains intentional |
| M11-03 / high | Extractor always emitted unresolved attribution, making live-source M9 account dossiers unavailable even for explicit speakers | Assertion schema permits a verbatim speaker label only inside the full quoted account. Independent provenance plus human semantic review still required. Live final run produced two attributable accounts and four M9 questions; regression fails without support | Substring provenance does not prove speaker semantics or personal knowledge. Humans must review attribution; labels never merge identities |
| M11-04 / medium | Installed checkpoint serializer warned that SourceIdentity/WorkflowBudgets/RecordDependency would be blocked in future; default permitted arbitrary extension classes | Exact supported-type allowlist through public JsonPlusSerializer API, pickle disabled. Legacy serializer wire-byte restart regression and unregistered-type non-reconstruction regression pass | Unknown extension types may become plain mappings and fail domain validation; do not add wildcard allowlists to bypass recovery errors |
| M11-05 / low | Wider suite initially 941 passed / one failed: legacy precedent API test constructed settings without M8 authentication | Test now uses explicit synthetic credentials and legacy fixture adoption. Production auth unchanged | No auth bypass introduced |
| M11-06 / medium, OPEN | Repeating identical POST start requests produces two independent investigations because M8 has no start request identity | Dedicated offline probe confirms two distinct threads. No new workflow/storage/idempotency architecture introduced | After unclear start response, list/reopen runs; do not blindly POST again. Product-level start idempotency remains follow-up work |
| M11-07 / high functional omission | Actual quote-only live sources had no M6 pairs even with a reviewed common participant: predicates were always null, while M6 requires exact descriptors | Extract source-verbatim subject/predicate/object anchors into existing M3 fields, all-or-none; reject unsourced structure. New red/green regression proves predicate reaches M6; full offline service probe reaches one candidate pair and rejects unavailable reasoning | No semantic normalization/lemmatization; text fallback remains valid. Final live acceptance is still blocked; source entailment requires human review |
| M11-08 / high acceptance blocker, OPEN | Last capped extended live attempt returned a failed invariant (AssertionError); exact stage was not retained in its old CLI failure envelope | Harness now retains safe stage/errors/candidate counts/reasoning codes on failure and returns nonzero for blocked/failed CLI runs. No claim that the live failure is fixed | Reproduce with the updated harness before pilot; do not infer that token caps, model behavior or workflow state caused it without evidence |

Quarantine diagnostics now expose safe reasons and structured counters instead of
only INVALID_CANDIDATE. Logging records proposed kind counts, accepted/rejected
counts, processed characters, substantive uncovered characters, verification
rejections and verified event counts. Smoke output also records chronology groups.
No quotes, paths, credentials, provider bodies or private excerpts are logged by
these diagnostics. Malformed entire schema responses remain incomplete windows,
not silently coerced candidates.

## Comparison with original empty-chronology incident

The existing real incident was not reopened or changed. Its signature was
reproduced using fictional records: WAITING_REVIEW, PARTIAL coverage, quarantine
warnings and no verified events. TXT separators explain the small uncovered gaps;
they do **not** explain invalid candidate provenance. The first diagnostic fix
removed false geometric PARTIAL but still yielded zero accepted records, proving
that merely relabeling coverage was insufficient. The quote-only/kind-specific
schema then yielded 13 verified records and three events in the genuine live run.
A subsequent extended probe exposed the independent M6 descriptor omission; its
source-structure adapter is regression-tested, but the final extended live run
failed. The earlier extraction/export success does not override that failure.

Model dates remain raw under existing M3 semantics. Dated chronology requires
reviewer-supported interpretation; the test harness explicitly supplied the date
from the quoted fictional account and then accepted the revised event. No dates,
quotes, witnesses or normalized events were fabricated to fill the prior gap.

## Integrity, security, recovery and Word assessment

Existing security/recovery suites plus the new real API chain exercise wrong
matter/thread ownership, cross-operator binding, anonymous access, stale/tampered
source text/versions/journal entries, unsigned publications, rejected/deferred
plans, invalid client review data, stale revisions and safe errors. Domain-specific
401/403/404/409/422/503 handling and no credential/path leakage remain intact.
M7 crash-boundary tests cover approval intent reconciliation without duplicate
publication; M9 retry/restart tests cover append-only decisions and approved
revision recovery. Published artifact reads use signed digest identity, not mutable
working caches. Coverage/quarantine warnings survive into downstream material.

DOCX checks are programmatic package/content/provenance checks, not a renewed
visual-layout audit. Existing tests cover headings, editable paragraphs, ordered
questions, exact quotes, source appendix, safe filenames, unusual Unicode/control
characters, empty reviewed sections, historical approved revisions and size bounds.
The live files were loaded with python-docx and their source content read back.
The stronger offline smoke checks every exported account quote and its complete
document/version/section identity, plus domain/checkpoint non-mutation. Fonts and
Word pagination remain client dependent; no visual/font polish was attempted.

## Validation commands and evidence

Use an activated project Python 3.12 environment. Final counts below are not
summed across overlapping suites.

```sh
pytest tests/unit/test_investigation_factual_extraction.py tests/api/test_exports.py -q
# Baseline: 38 passed.
pytest tests/unit/test_pilot_extraction.py tests/unit/test_investigation_durable.py tests/unit/test_investigation_workflow.py tests/integration/test_pilot_smoke.py -q
# Intermediate focused run: 110 passed (before later added negative/probe tests).
pytest tests/api/test_investigations.py tests/api/test_deposition_api.py tests/api/test_exports.py tests/api/test_operator_auth.py tests/integration/test_export_integrity.py tests/integration/test_deposition_recovery.py -q --tb=short
# Security/API/recovery selection: 120 passed, 0 failed.
pytest -q --tb=short
# Final full-suite count recorded below.
ruff check src tests scripts/pilot_smoke.py
ruff format --check src tests scripts/pilot_smoke.py
mypy src tests scripts/pilot_smoke.py
python -m pip check
git diff --check
python scripts/pilot_smoke.py
python scripts/pilot_smoke.py --live --model qwen3:8b
python scripts/pilot_smoke.py --live --model qwen3:8b --with-findings
# Extended live acceptance remains FAIL; use the updated diagnostics to investigate.
```

Test-first evidence: initial geometry/diagnostic tests **2 failed, 1 passed**;
explicit source-anchor/speaker tests then **2 failed, 3 passed** before their fix.
An intermediate wider run found the legacy auth-test failure described above;
that test subsequently passed with synthetic auth. The later source-predicate
regression also failed before the source-structure fix. Final focused smoke /
extraction checks: **11 passed, 0 failed**. Final full suite:
**948 passed, 0 failed (37.04 seconds)**. Ruff check/format pass (178 files); strict mypy passes
(178 source files); pip reports no broken requirements. Final `git diff --check` passed after the report was written.

Frontend source sync excluded `node_modules`, `.next`, `.git` and `*.tsbuildinfo`.
These checks ran once in the external checkout, all passed:

```sh
npm run typecheck
npm run lint
npm test -- --reporter=dot
npm run build
```

Vitest: **57 passed, 0 failed, 11 files**. Next.js compiled and generated routes
successfully. No frontend tool hung. No model download, remote legal research or
real client data was used. Offline fixtures do not count as live-provider tests.

## Dependency and migration assessment

Installed Python dependencies satisfy package metadata (`pip check` passed).
`python-docx>=1.2,<2` is a declared runtime dependency; installed 1.2.0 was used.
SQLite checkpoint saver remains pinned to 3.1.3. LangGraph/checkpoint and other
Python dependencies have compatible ranges rather than a complete Python lockfile;
only the frontend has a lockfile. Preserve the tested environment or capture a
release-specific dependency lock before deployment. No dependency or DB-schema
change was made by M11.

Existing supported checkpoint bytes recover under the explicit allowlist; no
migration is needed for those types. Unsupported/unversioned saver databases
remain intentionally rejected by the existing schema fence. If a real checkpoint
contains an unexpected historical type, back up the workspace and inspect a copy;
do not suppress warnings, allow whole modules or enable pickle to force recovery.
Old signed snapshots are not rewritten; improvements apply to fresh investigations.

## Pilot acceptance and operator actions

- [x] Genuine installed Ollama model exercised against isolated fictional data.
- [x] Verified source evidence survives; separate accounts and uncertainty retained.
- [x] Both TEST-ONLY review gates, signed publication and safe retries work.
- [x] M9 approved revisions, both DOCX downloads and restart recovery exercised.
- [x] Full automated regression and frontend quality gates run.
- [ ] Resolve M11-08 with the updated safe diagnostics. A nonzero-search run was
  attempted and failed; reproduce it, establish the cause, and rerun all affected
  checks after any fix. Do not simply raise budgets or bypass provenance.
- [ ] Complete a supervised synthetic case with explicitly reviewed participant
  links, bounded search and an independently verified finding. Exact source
  predicates do not establish that accounts describe the same event/person.
- [ ] Perform a live browser pass for upload, evidence/date/identity review,
  findings review, M9 revision conflicts and authenticated downloads.
- [ ] Review extraction recall, attribution and date interpretations manually;
  a COMPLETE coverage label is not a correctness certificate.
- [ ] Resolve or explicitly accept non-idempotent start behavior with a documented
  list/reopen procedure after uncertain network outcomes.
- [ ] Capture tested dependency versions and private-workspace backup/restore
  procedure before pilot installation. Never change operator identity/token or
  enable legacy adoption casually on an existing workspace.

The resulting readiness is **FAIL (acceptance blocked)**, not PASS. Live
inconsistency acceptance failed; live browser and deployment behavior remain
unverified. Code is available for review, but the system is not pilot-ready.
This run is not a security penetration test, legal review, extraction benchmark or
professional approval. No real matter should inherit TEST-ONLY approvals.

## Exact M11 file manifest

- `docs/product/PILOT_READINESS.md`
- `scripts/__init__.py`
- `scripts/pilot_smoke.py`
- `src/lextrace/investigations/durable.py`
- `src/lextrace/investigations/extraction_verification.py`
- `src/lextrace/investigations/factual_extraction.py`
- `src/lextrace/investigations/workflow.py`
- `src/lextrace/research/prompts.py`
- `tests/integration/test_pilot_smoke.py`
- `tests/unit/test_investigation_durable.py`
- `tests/unit/test_investigation_workflow.py`
- `tests/unit/test_pilot_extraction.py`
- `tests/unit/test_precedent_intelligence.py`

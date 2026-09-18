# LexTrace end-user product audit

Date: 2026-09-17. Scope: running local Docker app, current source, synthetic
Matter, one new synthetic Research request, case search, public competitor
documentation. This is an engineering/product audit, not a legal opinion or
retrieval-quality benchmark. Competitors were researched through their public
documentation; their authenticated products were not tested.

## Assessment

LexTrace has useful building blocks: source spans, case passages, persistent
Matters, claim review, evidence matrices, citation provenance, and background
jobs. It is not yet ready for reliable everyday end-user research. The main
gaps are truthful job state, semantic quality, corpus scope, workflow continuity,
and actionable error handling. Visual redesign alone will not resolve them.

Recommended initial user: a litigation associate reviewing a draft brief in a
supported jurisdiction. Primary outcome: an evidence-linked review report with
claims needing attention, reasons, sources, unresolved gaps, and reviewer notes.
Validate that persona with interviews before expanding scope.

## Live observations

| Test | Result | Interpretation |
| --- | --- | --- |
| Docker services and `/health` | Both services healthy; health returns `ok` | Process liveness does not establish research readiness |
| Provider settings | Ollama, qwen3:8b, 180-second request timeout | Previous OpenAI configuration mismatch is corrected |
| Prior synthetic document analysis | Completed in about 247 seconds; five claims present | A successful real local workflow can exceed the Research UI waiting limit |
| New Research request about truthful attorney advertising, jurisdiction ca2 | Browser displayed the reported configuration error; server still reported this run queued | Reproduced false failure, not a demonstrated provider configuration failure |
| Existing Research request | Still running approximately eleven minutes after submission at observation | No stage or heartbeat available to distinguish slow work from a stall |
| BM25 search: truthful attorney advertising | Returned Alexander v. Cahill first, plus relevant and unrelated cases | Functional retrieval; top excerpt was a list of advertising ethics references, not the strongest responsive passage |
| Existing Matter | Five claims and source-linked findings loaded | Initial loading attempt failed; reload recovered. Exact transport cause was not established |
| Evidence matrix | 15 default columns, raw case/document identifiers and enum labels | Hard to scan; exposes implementation vocabulary |
| Claim detail | Exact source text and case passages available | Valuable foundation for review, but buried below a long table |
| Monitoring | Requires a backend JSONL path and Stage 2 model checkbox | Operator workflow presented as end-user functionality |

The audit submitted one additional local Research job. It was queued at the
last status check; no successful memo is claimed. No CourtListener acquisition,
new paid-provider configuration, or destructive data action was performed.

## P0: repair before calling this a usable research product

### 1. Research reports false failure and loses continuity

`frontend/components/ResearchView.tsx` polls 120 times with one-second pauses,
then reports a configuration error regardless of whether the job is queued,
running, or failed. HTTP errors also collapse to the same message. The accepted
run ID is not persisted in a navigable result route. Resubmission creates another
job, which can worsen the queue.

`ResearchJobs` uses one worker by default. Its executor queue is not explicitly
bounded. The API does not wire the workflow observer to durable progress; the
default observer is null. The per-provider timeout is not an end-to-end deadline.
The workflow contains many sequential stages and permits repeated model calls.

Required change: route immediately to a durable run page; show queued/running/
completed/partial/failed separately, stage, elapsed time, last progress, and safe
failure category. Refresh must reconnect. Bound admission and total work; add
cancellation and deliberate retry/resume without duplicate submission. Persist
partial evidence. Do not simply increase a browser timeout or worker count.

Acceptance: a job queued longer than two minutes stays queued in the UI; a
four-minute run completes visibly; reload restores it; failures identify a safe
action; double-click/retry does not create duplicates; stalled work is detected.

### 2. Source verification is being mistaken for semantic verification

The synthetic claim “Truthful attorney advertising is protected by the First
Amendment” was labeled CONTRADICTED/VULNERABLE. One displayed counter passage
expressly described lawyer advertising as protected speech. This is an observed
semantic inconsistency requiring expert review, not a comprehensive assessment
of the governing law.

`matter/analysis.py` verifies evidence IDs and passage ownership, then accepts
model judgment labels. Any accepted CONTRADICTED judgment can determine the
overall result. Correct provenance alone cannot prove an interpretation.

Required change: separate exact-source verification, model interpretation,
retrieval role, and human review state. Present model assessments as proposed.
Distinguish contradiction, qualification/limitation, background, and uncertainty.
Require an inspectable explanation for each relationship and allow its correction
with provenance. Do not describe a passage merely retrieved by a counter-query
as a confirmed counter-authority.

Acceptance: lawyer-reviewed fixtures cover supporting passages, qualifications,
opposing holdings, quoted party arguments, negation, and no evidence. Measure
false contradiction rate and abstention separately from citation accuracy.
Run a bounded, repeatable local-model evaluation in addition to mocked tests.

### 3. Corpus coverage is hidden at the point of reliance

The current index contains 14 cases: seven ca2, five scotus, one ca5, one ca11.
Filing dates span 1895-03-04 to 2010-03-12. This is an engineering smoke corpus,
not a current or comprehensive U.S. case-law collection. General Research copy
does not expose those limits before submission.

Required change: visible corpus count, courts, date range, update date, source,
and missing coverage. Label this installation as a sample corpus. Separate “no
support found in these materials” from a claim being legally unsupported.
Make supported jurisdiction selection explicit and test controlling-authority
inclusion; workflow court filters currently depend on planner preferences or
the literal jurisdiction input.

Acceptance: out-of-scope requests receive a coverage explanation; no source
coverage implies no claim of exhaustive research; an as-of date is not presented
as proof that the corpus is current.

## P1: simplify the user journey

1. **Matter overview:** show documents, outstanding review tasks, active runs,
   and last completed work. Replace large marketing headers with compact working
   navigation after entry. Avoid showing zero counts while loading or on error.
2. **Guided intake:** choose “Review a brief” or “Research a question”; select a
   named jurisdiction and available sources; preview document extraction; explain
   OCR failures. Separate uploaded/ready/analyzed states visibly.
3. **Single claim review surface:** merge overlapping Matrix and X-Ray views.
   Default columns: claim, assessment, evidence, coverage, next action. Keep
   advanced columns optional. Replace `38` with a case name and UUIDs with titles.
4. **Evidence side panel:** clicking a claim opens its source excerpt and exact
   case passage alongside it, with reviewed status and a clear return path.
   Keep raw hashes, IDs, and character offsets behind provenance details.
5. **One research concept:** distinguish a standalone research question from
   “Investigate this claim,” but reuse run history and progress conventions.
   Present plan approval as meaningful scope review, not unexplained machinery.
6. **Search:** default to a sensible supported method; put BM25/dense/reranker
   selectors and raw scores in advanced diagnostics. Add court/date controls,
   save-to-Matter, passage context, and explicit source coverage.
7. **Alerts:** lawyers choose matters/authorities to watch and review changes.
   Snapshot paths, graph rebuilds, and Stage 2 settings belong in administration.
   Do not imply continuously refreshed law while updates are manual snapshots.
8. **Work product:** retain the existing matrix CSV, then provide an editable
   review report/memo with source links, unresolved items, scope, and reviewer
   decisions. Prioritize a useful export before building Word integrations.
9. **Errors and recovery:** centralized safe error codes, retry actions, and
   distinct loading/empty/error states. Current Matter refresh uses Promise.all;
   one failed core request suppresses all core state. Matrix/monitoring failures
   can be displayed as empty data. Claim research errors can incorrectly say an
   already analyzed claim needs analysis.
10. **Accessibility:** test keyboard flow, focus movement to detail panels,
    announcements for job progress, tab semantics, small-screen tables, and
    readable status labels. These require explicit validation; this audit did
    not establish comprehensive accessibility compliance.

## Market comparison

These are documented product patterns, not independently verified vendor
accuracy or performance claims.

| Product | Documented pattern | Practical lesson for LexTrace |
| --- | --- | --- |
| Harvey | Assistant connected with Vault, workflows, knowledge sources and reusable work | Keep document context and source selection connected across tasks |
| CoCounsel | Research plans and structured reports with a research log | Explain work performed and deliver a reviewable output, not a spinner |
| Legora | Source-linked research and an editor that exports to Word | Close the loop from evidence to usable work product |

Sources inspected:

- [Harvey getting started](https://help.harvey.ai/articles/getting-started-with-harvey)
- [Harvey Vault](https://www.harvey.ai/platform/vault)
- [CoCounsel research transparency](https://legal.thomsonreuters.com/en/insights/white-papers/how-trustworthy-content-transforms-legal-deep-research)
- [Legora legal research](https://legora.com/product/legal-research)
- [Legora Editor](https://legora.com/product/editor)

Avoid attempting full Harvey feature parity. A defensible first product is a
reliable, auditable brief-review workflow over an honestly described collection.
Broader source licensing, collaboration, DMS/Word integrations, and multi-tenant
deployment require separate product, security, and operational work.

## Delivery order and release gates

Audit checks: all nine frontend tests passed. A targeted backend pytest run
covering Research API/runtime, Ollama, and Matter analysis was interrupted after
approximately 149 seconds without completing; the interrupt surfaced during
module import. Its cause was not established and no backend pass is claimed.
`git diff --check` passed. This audit changed documentation only and is not a
fresh full-suite release validation.

1. **Reliability:** durable job UX, safe diagnostics, provider readiness, queue
   limits, deadlines, restart handling. Gate: no false failures or lost runs.
2. **Trust:** visible corpus scope and evidence/interpretation distinction;
   adjudicated semantic regression set. Gate: agreed quality thresholds based
   on reviewed examples, not a count of passing mocked tests.
3. **Workflow:** compact Matter shell, guided intake, unified review surface,
   named authorities, evidence panel, saved research and useful export. Gate:
   target users complete upload → review → export without developer assistance.
4. **External pilot:** realistic representative data; privacy/access controls,
   retention/deletion and operational monitoring appropriate to deployment.
   Measure task completion, false contradiction rate, citation correctness,
   time to first evidence, total run time, and recovery success.

Do not enlarge the corpus or add more model-driven stages before addressing
the current misleading states. Do not change source-text preservation rules.
The findings warrant targeted repair and user testing, not another feature list.

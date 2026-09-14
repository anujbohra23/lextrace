# Deep Research, Red Team, and Evidence Matrix v2

LexTrace v2 follows the Matter claim workflow: source span → Argument X-Ray →
precedent analysis → coverage assessment → reviewed research plan → bounded
research → adversarial check → Evidence Matrix → lawyer review. The workspace
keeps claim support, research coverage, and attack severity as separate signals.

## Coverage and gaps

`ResearchCoverage` reports observed signals in the **currently indexed corpus**.
Institutional court hierarchy does not prove issue fit. Unverified retrieval hits
count as authorities retrieved, not as supporting or contrary decisions. A
verified support/counter relationship comes from the existing claim finding.
`SUFFICIENT` means the configured local checks passed; it never means all
relevant law was found. Missing citations, absent verified passages, lack of
controlling authority, and lack of contrary authority produce typed gaps. Other
gap types remain available for reviewed later-treatment and fact-specific
workflows; the system does not manufacture them from search-result scores.

## Approval and limits

`POST /matters/{matter_id}/claims/{claim_id}/research-plan` creates a draft.
Reviewers edit the proposed queries, include or remove steps, and approve with
`PATCH` before `POST .../deep-research` can start. Claim edits invalidate that
claim's plan; a stored fingerprint prevents execution of a stale plan. A
background job persists its run ID and rounds. Defaults cap rounds at 2,
queries per round at 3, results at 30, duration at 90 seconds, and LLM/graph
calls at zero in Deep Research. The plan schema enforces upper bounds. A round
with no new case or passage stops early; exhausting bounds returns
`LIMIT_REACHED`. Stopping a run is explicit. There is no autonomous hidden plan
execution by default.

The separate LangGraph research boundary calls the existing retrieval engine.
Each discovery retains its query intent, step, gap, and round. Search hits are
not silently promoted into verified legal support. Re-analyzing a claim through
the existing verifier remains necessary to judge newly found passages.

## Adversarial review

Red Team runs two bounded counter searches and, when a configured structured
model is available, asks it to judge supplied exact passages. Deterministic
checks reject invented case and passage IDs. One bounded factual comparison may
identify an exact Matter document span and, for a distinction, an existing case
passage. Source sections are treated as untrusted data. Without a configured
model, counter-search hits remain unjudged. Verified attack provenance is
necessary for inclusion in the Matter attack surface; the lawyer still reviews
legal fit and factual interpretation. Severity is categorical and is **not** an
outcome prediction.

## Storage and review

Versioned Matter SQLite tables persist plans, coverage, runs, and attack
findings under ignored runtime paths. Claim edits and authority review clear
only affected derived research state. A wording lock preserves the claim while
preventing accidental text edits. Generated Matter documents and artifacts
remain private. The Evidence Matrix v2 is a projection of canonical claims,
findings, coverage, and verified attacks; CSV contains structured references,
not private binary documents, and guards spreadsheet formula cells. The matrix
supports server-side filters and sorting through `/evidence-matrix`.

This feature is an engineering workflow over an incomplete local corpus. It does
not assert comprehensive precedent coverage, automate final legal judgment, or
calibrate litigation risk.

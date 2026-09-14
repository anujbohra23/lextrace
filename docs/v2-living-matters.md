# Living Matters and Precedent Monitoring v1

Monitoring compares two **prepared, normalized Case corpora**. It does not poll CourtListener or discover source updates on its own. A Matter contains explicit claim, authority, issue, doctrine, or Matter targets; high-importance claims can be selected automatically. Targets and the bounded policy are inspectable in the Matter workspace.

```text
Matter claims and authorities → monitoring targets
old Case snapshot + new Case snapshot → deterministic corpus delta
delta + citation graph + BM25 → bounded candidates → exact passages
impact evidence → verified events → deduplicated alerts → lawyer review
approved alert → one claim's evidence, coverage, and derived caches updated
```

Use `lextrace update-corpus --base data/cases.jsonl --batch data/new-cases.jsonl --output data/cases-v2.jsonl --index-output artifacts/indexes/v2 --bm25-only` to merge by source ID and publish an immutable JSONL snapshot. Add `--old-citation-source`, `--new-citation-source`, and `--graph-output` together to merge raw opinion citation evidence and build a new graph. Changed existing cases are reported separately from added cases. The BM25/dense index and graph currently undergo explicit full rebuilds; these flags do not imply incremental indexing. All snapshots and build artifacts remain Git-ignored.

The API accepts a prepared `new_corpus_path` under `data/` at `POST /monitoring/run`, with optional `matter_ids`, graph paths, and hard limits. Poll `GET /monitoring/runs/{run_id}`. Matter endpoints provide policy and target edits, alerts, review, application, and an approval-gated Deep Research handoff. The frontend Alerts tab exposes run history and exact new-authority passages. It does not silently start monitoring.

The local scheduler is manual/background execution; a future periodic caller can invoke the same service. Runs record old/new hashes, graph identities, candidate/event/alert counts, limits, errors, and elapsed time. A run can correctly end `NO_MATERIAL_CHANGE`. Stable alert IDs prevent duplicate alerts for the same case, claim, and event type; material reclassification increments an alert version while preserving review state.

Only locally present cases can be screened. BM25 and graph citation matches are candidate signals, not legal conclusions. Optional structured judgments are validated against exact case and passage IDs and quoted text; without a configured judge, semantic matches remain informational. Treatment language is unreviewed until a lawyer confirms the annotation. Only a confirmed limiting/overruling treatment can independently elevate a weakening alert. An alert cannot be applied as support or opposition until its effect has been assessed. Applying affects the cited claim and invalidates its derived research and doctrine state; unrelated Matters remain untouched. Alerts do not predict legal outcomes.

V1 limits new cases, Matters, candidates per target, Stage 2 analyses, LLM calls, and runtime. The current API uses no LLM judge and makes no provider calls. Graph-edge comparison is local and bounded per case; it is not a complete historical graph migration audit. Changed source records and graph-only updates require explicit review/reindexing before they can be treated as new authority. Synthetic golden fixtures in `tests/fixtures/monitoring_golden.json` cover twelve monitoring scenarios without network access.

# Living Matters and Precedent Monitoring v1

Monitoring compares two normalized Case corpora. It does not poll CourtListener continuously. A Matter contains explicit claim, authority, issue, doctrine, or Matter targets; high-importance claims can be selected automatically. Targets and the bounded policy are inspectable in the Matter workspace.

```text
CourtListener bounded batch → immutable Case snapshot → full index/graph rebuild
old/new corpus and graph versions → delta → monitoring targets
BM25 and watched-authority graph edges → bounded candidates → exact passages
structured impact judgment → deterministic evidence check → event → alert
lawyer review → one claim's evidence, coverage, and derived caches updated
```

Use `lextrace update-corpus --base data/cases.jsonl --batch data/new-cases.jsonl --output data/cases-v2.jsonl --index-output artifacts/indexes/v2 --bm25-only` to merge by source ID and publish an immutable JSONL snapshot. Add `--old-citation-source`, `--new-citation-source`, and `--graph-output` together to merge raw opinion citation evidence and build a new graph. Changed existing cases are reported separately from added cases. The BM25/dense index and graph currently undergo explicit full rebuilds; these flags do not imply incremental indexing. All snapshots and build artifacts remain Git-ignored.

For one local refresh and monitoring run, use:

```sh
lextrace refresh-monitoring --base data/cases.jsonl --court ca2 \
  --max-cases 5 --request-interval 15 --output data/cases-v2.jsonl \
  --index-output artifacts/indexes/v2 --bm25-only
```

The command reuses bounded CourtListener ingestion and writes a resumable `.batch.jsonl` and manifest beside the output. `--resume` continues that batch. Optional `--matter-id` restricts the run. Optional old/new citation source and graph paths use the existing graph builder; without new citation evidence the old graph remains the monitoring baseline. A request-level ingestion failure preserves completed batch records and manifest and does not publish a new corpus. Snapshot and index output paths are immutable: a differing rerun needs a new version path. The printed report includes version identities, delta, run ID, alert count, ingestion quality, and honest rebuild strategies. An API caller can still run monitoring directly over an already prepared `data/` snapshot.

The API accepts a prepared `new_corpus_path` under `data/` at `POST /monitoring/run`, with optional `matter_ids`, graph paths, and hard limits. Poll `GET /monitoring/runs/{run_id}`. Matter endpoints provide policy and target edits, alerts, review, application, and an approval-gated Deep Research handoff. The frontend Alerts tab exposes run history and exact new-authority passages. It does not silently start monitoring.

The local scheduler is manual/background execution; a future periodic caller can invoke the same service. Runs record old/new hashes, graph identities, candidate/event/alert counts, limits, errors, and elapsed time. A run can correctly end `NO_MATERIAL_CHANGE`. Stable alert IDs prevent duplicate alerts for the same case, claim, and event type; material reclassification increments an alert version while preserving review state.

Only locally present cases can be screened. BM25 and graph citation matches are candidate signals, not legal conclusions. Incoming citation evidence for watched authorities is compared even if no Case was added. New evidence can trigger a graph-only run; removed evidence is recorded without automatically making a contrary legal claim. A subsequent human-confirmed treatment review can reclassify an existing alert without a new corpus or graph version.

Stage 2 sends a bounded claim, prior finding evidence, exact new passage, court relationship, citation provenance, treatment review state, doctrine snapshot, and coverage gaps to the existing `StructuredLLM` adapter when `LEXTRACE_LLM_MODEL`, `OPENAI_API_KEY`, and positive LLM/token run limits are configured. By default no model is called. The model returns a typed category with case ID, passage ID, exact quote, and evidence IDs. Deterministic checks reject invented IDs, wrong passage ownership, inactive targets, stale run evidence, and unreviewed treatment used as a substantive premise. Insufficient/no-effect judgments produce no alert; unsupported substantive judgments fall back to informational review. Severity follows court and confirmed-treatment rules, not model choice. Only a confirmed limiting/overruling treatment can independently elevate a weakening alert. An alert cannot be applied as support or opposition until its effect has been assessed. Applying affects the cited claim and invalidates its derived research and doctrine state; unrelated Matters remain untouched. Alerts do not predict legal outcomes.

V1 limits new cases, Matters, watched authority IDs, candidates per target, Stage 2 analyses, LLM calls, output tokens per provider call, and runtime. Reported total token usage is checked between calls; exact provider-side input tokens cannot be hard-capped without a model tokenizer. Graph comparison is bounded to watched authorities and up to 100 incoming edges per authority; it is not a complete historical graph migration audit. Changed source records are reported separately and not treated as newly added authority. Synthetic golden fixtures in `tests/fixtures/monitoring_golden.json` cover twelve scenarios without network access.

The standard Docker backend pins a CPU PyTorch wheel from the official CPU wheel index before installing LexTrace retrieval extras. This prevents pip from selecting CUDA dependencies on Linux while leaving the macOS editable-install path unchanged. The CPU wheel and package resolver remain checked by a real Compose build and health test.

Stage 2 generation uses an explicit ledger of canonical claim, case, passage, and
evidence IDs. Positive impact judgments must include their candidate passage in
`evidence_ids`; no-effect and insufficient-evidence judgments may leave that list
empty. An untrusted generation draft may receive one reference-only repair before
typed and run-local reference validation. The deterministic provenance and legal
checks still decide acceptance. Relevance alone remains informational.

Applying substantive evidence invalidates the affected claim's previous support
and vulnerability assessment, Doctrine state, and research/attack caches. The
evidence and authority links remain available, and coverage is reassessed. Normal
claim reanalysis is required before relying on a new assessment; unrelated claims
are unchanged.

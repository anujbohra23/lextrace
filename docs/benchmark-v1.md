# V1 Citation-Recovery Benchmark

## Task and interpretation

Given a citation-scrubbed facts/legal-context excerpt from a judicial decision,
rank earlier decisions and measure recovery of cases actually cited by the source
opinion. These are **citation-derived positives**, **weak labels**, and **observed
cited precedents**. They are not comprehensive legal relevance ground truth.
Uncited candidates are **unjudged candidates**, not proven negatives.

V1 targets 25 Second Circuit queries filed during 2010 and a shared pool of
200–500 candidate cases, targeting 300. Candidates come from the Second Circuit
and U.S. Supreme Court, filed on or before 2009-12-31. Every query needs at least
two eligible citation-derived positives. A local BM25 evaluator is implemented;
the live V1 bundle is not complete (see the acquisition report below).

## Conservative temporal universe

The fixed pre-2010 cutoff gives every query the same historical candidate
universe, prevents temporal leakage, and improves comparability. It deliberately
excludes otherwise valid precedents filed earlier during 2010. Do not revise the
cutoff after observing retrieval results; a different rule requires a new version.
The builder separately verifies every candidate date is strictly earlier than
every query date. Missing query dates fail validation; missing target dates are
excluded with a recorded reason.

## Inputs and citation evidence

The builder is offline. Supply a frozen Case JSONL containing query cases and an
eligible candidate sampling frame, plus a BenchmarkInputs JSON file marked
reviewed or review_required.
Underlying Case and Opinion models and their text normalization are unchanged.
No full Case objects appear inside BenchmarkItem records.

Citation construction is explicitly:

    selected source opinion -> cited opinion ID -> cited cluster ID -> Case.source_id

Opinion IDs and cluster IDs are not interchangeable. Reporter citations identify
a decision and remain separate from precedent citation relations. Each selected
lead (`020lead`) or combined (`010combined`) opinion needs a complete outgoing
relation list. Dissent-only sources are not selected. Each cited opinion needs an
explicit mapping or an explicit unresolved/ambiguous/unavailable record. Duplicate
opinion citations and multiple opinions in one cluster collapse to one positive
cluster. Retain complete evidence, not a hand-picked subset of citations.

Positive exclusions have fixed codes: `self`, `same_litigation`, `unresolved`,
`missing_date`, `out_of_scope_court`, and `after_cutoff`. Mapping conflicts fail the
build. An eligible resolved target missing from the frozen Case corpus fails the
build rather than silently reducing the labels. All eligible positives must be
present in the shared candidate set. Adverse treatment does not remove a positive.
Citation depth is retained in upstream metadata when available, not used as a
relevance grade or sampling weight.

`ingestion/benchmark_sources.py` parses already-frozen CourtListener opinion
metadata (`cluster`, `opinions_cited`) or `opinions-cited` pages. It makes no HTTP
requests. A page with `next` is incomplete; acquisition must collect every page
before asserting `complete: true`. Payload hashes identify source responses;
the builder records them but cannot authenticate an upstream response from a hash
alone. Acquisition preserves raw successful JSON responses in a local hash-checked
cache. Citation evidence records ordered page hashes and per-opinion depths.

## Human-selected queries and mechanical scrubbing

Select 150–300 whitespace-delimited words of facts, procedural context, or legal
question, preferably before examining positive labels. Avoid citation-heavy
analysis and retrospective holdings where possible. Record the selection note.

QuerySelection records source cluster/opinion IDs, the SHA-256 of the normalized
source text, and half-open Unicode character offsets `[start, end)`. Removal
spans use absolute offsets in that same text. They must be disjoint and contained
within the excerpt, with a fixed reason such as `case_reference` or
`citation_bearing_clause`. The builder removes spans, joins retained segments
with spaces, and collapses whitespace. It never rewrites the underlying text.
No citation-count markers, summaries, spelling corrections, or LLMs are used.

The builder checks exact known names/aliases and reporter citation strings from
all mapped cited decisions, including excluded targets. It also screens obvious
short forms (`Id.`, `Ibid.`, `supra`, `infra`), `v.` case references, and common
federal reporter patterns. These checks are intentionally incomplete: aliases,
OCR errors, quotations, and paraphrases can leak identity. All 25 queries require
named human leakage approval and confirmation that known aliases were reviewed
before the bundle can have reviewed status.
Mechanical success is not a guarantee of zero leakage.

## Candidate sampling

Start with the complete union of eligible positives. Never truncate that union.
If it exceeds 500, the build fails. Fill remaining slots from the supplied frozen
eligible Cases using court and filing year as strata. Within each stratum, order
IDs by SHA-256 of `seed:court:year:case_id`, with numeric ID tie-breaking. Allocate
one distractor per stratum per round in sorted stratum order until the target is
reached. There is no reliance on Python's randomized hash or input row order.

The actual size is the larger of target size and positive-union size, capped by
the available eligible pool. Fewer than 200 eligible Cases fails the build.
Freeze and retain the sampling frame: a seed cannot reproduce a changing frame.
Objective stratification improves diversity but does not guarantee difficult or
topically relevant distractors. V1 does not claim to test hard-negative retrieval.
No taxonomy, BM25, embeddings, or topical mining is used for construction.

## Split and audit

Sort query cases by `(date_filed, numeric cluster ID)`: first 10 are dev, next 15
are test. Record identified related-litigation groups. If a group crosses that
boundary, fail and revise query selections before freezing; do not silently
change counts, dates, or move cases after viewing results. Group identification
requires human review and is not inferred solely from docket numbers.

For a reviewed bundle, all 25 queries need leakage review. Exactly 15 additionally
require detailed
positive-context audits covering every eligible positive once. Each audit records
a reviewer, source citation-context offsets, a note, and one category:

- substantive
- procedural
- background
- adverse treatment
- uncertain

Choose the detailed-audit sample before retrieval, spanning dev/test, dates, and
positive counts. Inspect citation resolution, earlier dates, excerpt coherence,
and several sampled unjudged candidates. A second reviewer is preferred; report
single-reviewer limitations. Audit categories characterize weak labels and do not
change binary gains. Never remove difficult positives based on retrieval results.

## Reproducible artifacts and commands

Version `experiments/configs/benchmark_v1.json` and this design in Git. Future
reviewed inputs and generated bundles stay under ignored `data/benchmarks/v1/`.
Example commands after data acquisition and human review are completed:

```sh
lextrace build-benchmark \
  --corpus data/benchmarks/v1/source_cases.jsonl \
  --inputs data/benchmarks/v1/reviewed_inputs.json \
  --config experiments/configs/benchmark_v1.json \
  --output data/benchmarks/v1/frozen
lextrace validate-benchmark data/benchmarks/v1/frozen
```

The builder refuses existing output, assembles everything before publication,
and renames a completed temporary directory into place. Use one writer per output.
The bundle contains:

- `sources.jsonl`: frozen source Cases, including full query opinions; provenance only.
- `candidates.jsonl`: the only corpus a future retrieval index may consume.
- `queries.jsonl`: typed BenchmarkItem records; no full Cases.
- `provenance.json`: reviewed selections, offsets/removals, audits, mappings, and edges.
- `config.json`: exact construction configuration.
- `audit.json`: every query excerpt, positives, depth, exclusions, and review status.
- `manifest.json`: frozen IDs, file hashes, generation-code content hash, versions,
  exclusions/counts, strata, audit IDs, and split assignments.

Serialize sorted keys, canonical IDs, UTF-8, and LF endings without timestamps or
machine paths. The manifest hashes relevant generation code rather than using
current time or Git dirty-state. Validation reconstructs every artifact from
frozen inputs and compares bytes. A code change requires the matching code version
to reproduce an old bundle. Hashes detect changes, not fraudulent annotations.
Never index `sources.jsonl` or pass positive IDs/provenance to a retriever.

## Acquisition and review-ready workflow

`acquire-benchmark` uses nonsemantic Search `type=o`, `court`, `filed_after`,
`filed_before`, `stat_Published=on`, and `order_by=dateFiled asc`. Cached pages
freeze the discovery frame; IDs are sorted by date then numeric ID. REST detail
requests select fields explicitly. Search timeout/server errors permit a narrow
one-day REST fallback; authentication, quota, schema, and network failures stop.
The fallback has unit coverage but has not been needed in a successful live run.
The server-side cause of the earlier broad cluster-list timeout is unknown.

All requests reconstruct trusted API paths, disable redirects, use a configurable
90-second acquisition timeout, and run sequentially with a configurable interval
(default 15 seconds). There are no retries. Quota is checked first when available;
429 reports a safely parsed Retry-After and stops. A per-invocation request ceiling
bounds cost. One writer per cache/output; do not refresh a frozen cache implicitly.
Successful resources have SHA-256 sidecars; corrupted cache files fail closed.
Resume retains completed source/provenance files and reuses valid cached responses.
Request journals are local operational metadata, not immutable server snapshots.

The first 25 eligible, distinct-name query cases with usable excerpt proposals
are selected in filing-date/ID order. Exact-name deduplication is conservative,
but does not identify every related proceeding; human litigation review remains
required. Invalid source records have safe rejection entries. Request failures
leave incomplete work explicitly incomplete. Do not interpret pending mappings
as exclusions, empty citation lists, or negatives.

Generation rules v1.1 declare a bounded distractor discovery frame **before any
retrieval experiment**: up to the first 60 published clusters per court/year,
2000–2009, sorted by date/ID from cached Search pages. Fixed-seed hash ordering
and court/year round-robin select text fetches. This frame controls REST cost;
it is not a random sample of all historical decisions and can overrepresent early
dates within a year. All eligible positives remain eligible regardless of this
frame's lower date bound. The builder includes their complete union and never
truncates it. Target counts, minimum positives, and the common cutoff are unchanged.

Automated excerpt proposals take the first passing window of up to 260 source
words at paragraph starts in the first half of the opinion. Known cited names,
reporter strings, and detectable reporter/short-form references are removed as
recorded spans. The 150–300 word and existing leakage checks still apply. No prose
is invented. All such selections have `review_status: review_required`, no human
reviewer, and no claimed audit. This is a proposal heuristic, not a coherence or
legal relevance judgment. Human review may choose different source spans.

A full provisional bundle may be built and evaluated, but its test and overall
numbers are **PROVISIONAL**. Promotion to reviewed requires the existing 25
leakage approvals and 15 complete context audits; changing a status string alone
cannot bypass those checks. Neither an incomplete pilot nor synthetic fixtures
constitute a real benchmark.

See [the BM25 implementation and live acquisition report](retrieval-baseline.md)
for commands, actual results, costs, and the remaining external blocker.

## Milestone 3B metric contract and limitations

For each query, binary gain 1 means an eligible observed cited precedent. Other
candidates receive gain 0 for computation while remaining unjudged. Recall@5,
Recall@10, and Recall@20 divide unique recovered positives by all eligible positives
for that query. MRR uses the first positive rank. NDCG@10 uses binary discounted
gains and the ideal ranking for that query's positive count. Macro-average over
queries; validate ranked IDs and reject duplicate results. No training split is
needed. Tune only on dev; keep test frozen. The implementation rejects duplicate
ranked IDs and reports dev/test/overall macro metrics.

The small court/date scope, sampled universe, citation-extraction errors, manual
excerpt selection, combined-opinion ambiguity, shared positives, and false
unjudged negatives limit generalization. Post-decision excerpts can still reveal
retrospective reasoning. Report these limits and do not treat scores as measures
of comprehensive legal relevance, binding authority, or legal research quality.
The local BM25 baseline implements this metric contract. Dense retrieval,
rerankers, graph/database infrastructure, LLM queries/labels, authority ranking,
frontends, and topical hard-negative mining remain separately deferred.

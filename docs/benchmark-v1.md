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
two eligible citation-derived positives. No retriever or metrics are implemented
in Milestone 3A.

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
eligible candidate sampling frame, plus a reviewed BenchmarkInputs JSON file.
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
alone. Preserve raw responses in the eventual acquisition archive. No acquisition
command is introduced in this milestone.

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
named human leakage approval and confirmation that known aliases were reviewed.
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

All 25 queries need leakage review. Exactly 15 additionally require detailed
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
- `manifest.json`: frozen IDs, file hashes, generation-code content hash, versions,
  exclusions/counts, strata, audit IDs, and split assignments.

Serialize sorted keys, canonical IDs, UTF-8, and LF endings without timestamps or
machine paths. The manifest hashes relevant generation code rather than using
current time or Git dirty-state. Validation reconstructs every artifact from
frozen inputs and compares bytes. A code change requires the matching code version
to reproduce an old bundle. Hashes detect changes, not fraudulent annotations.
Never index `sources.jsonl` or pass positive IDs/provenance to a retriever.

## Acquisition plan and live assumptions

Prefer a small REST extension for the eventual acquisition stage. Reuse local
Cases, deduplicate cited opinion IDs, cache opinion-to-cluster mappings, request
only needed metadata, and download candidate text after selecting frozen IDs.
Use existing conservative pacing and 429 stop behavior. No retries or bulk import
are added here. Bulk snapshots and a bulk citations map can be considered for a
later larger version; importing all opinion text is disproportionate for V1.

Before acquiring data, validate the live shape and completeness of `opinions_cited`,
`cluster`, citation-page `depth`/`next`, and lead/combined type codes. Verify that
2010 ca2 queries yield enough resolvable pre-2010 ca2/scotus targets under the
200–500 limit. Metadata availability, quota costs, and related-litigation coverage
remain empirical questions. Do not claim real benchmark completion from synthetic
tests or fabricate human approvals.

## Milestone 3B metric contract and limitations

For each query, binary gain 1 means an eligible observed cited precedent. Other
candidates receive gain 0 for computation while remaining unjudged. Recall@5,
Recall@10, and Recall@20 divide unique recovered positives by all eligible positives
for that query. MRR uses the first positive rank. NDCG@10 uses binary discounted
gains and the ideal ranking for that query's positive count. Macro-average over
queries; validate ranked IDs and reject duplicate results. No training split is
needed. Tune only on dev; keep test frozen. Metrics are not implemented yet.

The small court/date scope, sampled universe, citation-extraction errors, manual
excerpt selection, combined-opinion ambiguity, shared positives, and false
unjudged negatives limit generalization. Post-decision excerpts can still reveal
retrospective reasoning. Report these limits and do not treat scores as measures
of comprehensive legal relevance, binding authority, or legal research quality.
Milestone 3B may add a baseline retriever and evaluation metrics. Dense retrieval,
rerankers, graph/database infrastructure, LLM queries/labels, authority ranking,
frontends, and topical hard-negative mining remain separately deferred.

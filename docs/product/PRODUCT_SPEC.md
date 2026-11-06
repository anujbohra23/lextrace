# LexTrace Investigations — V1 Product Specification

Status: proposed design, based on repository inspection on 2026-11-05. This document does not describe shipped investigation capabilities. Existing case-law research, Argument X-Ray, precedent intelligence, and monitoring remain supported. See [architecture](ARCHITECTURE.md) and [milestones](IMPLEMENTATION_PLAN.md).

## Objective and user

Help a lawyer turn a bounded collection of matter documents into a reviewed, source-linked witness deposition preparation pack. The product organizes evidence and proposes investigative questions; it does not determine credibility, establish that an allegation is true, or infer legal treatment from citations.

Primary user: a lawyer preparing to depose a named witness. V1 targets a private, single-host workspace for one trusted operator. Shared deployments, authentication, tenant authorization, and production data governance require a separately scoped release; matter scoping alone is not user authorization.

## Flagship workflow

Create Matter → Upload Documents → Extract Facts and Events → Build Evidence Chronology → Investigate Potential Inconsistencies → Human Review → Generate Witness Deposition Preparation Pack → Export DOCX.

| Stage | User-visible behavior | Completion condition |
| --- | --- | --- |
| Matter | Reuse name, reference, forum, jurisdiction, and as-of date; add an investigation workspace | Existing research remains accessible |
| Documents | Upload PDF, DOCX, TXT, or Markdown; see extraction status and coverage | Immutable originals and extracted-text versions are available; failures are explicit |
| Extraction | Propose people, organizations, attributed factual assertions, and dated events | Every usable candidate has an exact source reference; unsupported candidates are quarantined |
| Chronology | View events in date order, with unknown dates separately; open supporting passages | Ambiguous dates and conflicting accounts remain visible |
| Investigation | Propose bounded discrepancies with both accounts, context, alternatives, and follow-up questions | Findings are labeled potential inconsistencies and carry verified evidence |
| Human review | Accept, reject, edit, or defer facts, identities, events, and findings | Reviewer explicitly approves an immutable investigation snapshot |
| Witness pack | Select witness, scope, topics, and relevant reviewed evidence | Draft references approved facts/events/findings and passes provenance checks |
| Export | Review the draft, approve its exact revision, and download DOCX | Export uses the approved snapshot and retains resolvable source locators |

## V1 scope and limits

Reuse the current 10 MB upload and 500-page PDF limits. Support text-bearing PDFs and existing DOCX/TXT/Markdown extraction. Scanned PDFs are marked OCR_REQUIRED; automatic OCR, email/archive ingestion, audio/video, handwriting, and sophisticated table reconstruction are deferred. Do not silently classify partial extraction as complete: show page/section coverage, extraction warnings, and skipped content. DOCX page numbers are unavailable; use paragraph/section and character references.

Proposed initial run limits: 100 ready documents and 2 million extracted characters; process at most 20,000 characters per extraction call with overlap and explicit coverage tracking; at most 200 inconsistency candidate pairs, 20 evidence-search calls, and one repair pass per invalid structured response. Stop at the configured time/token budget and expose partial coverage. Larger collections require explicit limits and performance work, not silent truncation. These are engineering bounds to validate, not demonstrated capacity.

Witness selection comes from reviewed entity identities or a manually entered witness profile. Alias merges require review. Unknown involvement or personal knowledge is an open question, never an inferred fact.

Optional case-law context uses the existing local retrieval and verification services. It is a separate user-selected action, not a prerequisite for a factual chronology or witness pack. No automatic CourtListener acquisition or quota use is introduced.

## Evidence and review rules

- A fact is an attributed assertion in a source, not an adjudicated truth. Distinguish exact quotes, normalized assertions, event interpretations, and investigative hypotheses.
- Preserve original bytes and extracted source text, including defects. Record extraction identity and hashes; never repair spelling or dates in the source layer.
- Every factual assertion and factual premise in a question references run-local document-version spans. Every legal proposition references run-local canonical case and opinion passages. A source match proves provenance, not semantic truth.
- Potential inconsistencies require two distinct sourced assertions, an identified comparable subject/predicate/time scope, and an explanation of competing interpretations. Single-source gaps are follow-up questions, not contradictions.
- Human review and deterministic provenance validation are independent gates. Acceptance cannot override a missing source or mismatched quote. An accepted inconsistency remains a reviewed investigative hypothesis.
- Rejected, deferred, stale, and unverified material cannot become affirmative pack content. Open questions can describe an approved uncertainty; they cannot smuggle in unsupported premises.
- Edits preserve prior revisions and reviewer attribution. Relevant evidence, identity, date, or wording changes invalidate downstream approval and exports for the new revision. Historical approved exports stay immutable and are labeled superseded.

## Review experience

Extend the existing matter workspace with Investigation, Chronology, Review, and Witness Pack views. Reuse upload status, polling, and the source viewer. Each review item shows source excerpts, attribution, uncertainty, generator/version, and decision history. Chronology supports witness/document/date filters and unknown or overlapping date ranges. Inconsistencies show both accounts side by side and allow rejection as compatible context or separate events.

Review has two required gates: approve the investigation snapshot before pack generation, then approve the exact verified pack revision before export. A reviewer may exclude an item without resolving it; the pack lists resulting coverage limitations. No unreviewed item is implicitly approved by a bulk action. Optional early identity/date review can reduce downstream ambiguity.

## Deposition preparation pack

The DOCX contains matter and witness identifiers, preparation date, snapshot/run/pack revision, reviewed witness background, scope and coverage limitations, source-linked chronology, topic outline, proposed questions, reviewed potential inconsistencies with both accounts, exhibit/source index, and open follow-up items. Question types include foundation, sequence, clarification, authentication, and discrepancy exploration. Separate optional legal context from factual evidence.

Each question stores its topic, purpose, supporting fact/event/finding IDs, and evidence references for every factual premise. Neutral questions without factual premises may have no source. Proposed exhibit labels are internal preparation labels, not representations that an exhibit has been admitted or formally numbered. Include filename, version/hash prefix, page when known, section and character range, and a source key that resolves in the local workspace; avoid embedding private filesystem paths. DOCX links supplement readable locators and must not be the sole provenance mechanism.

## Acceptance and evaluation

Release requires an offline synthetic end-to-end matter with two accounts, an ambiguous date, an alias, a scanned PDF, and a malicious instruction embedded in a document. A reviewer can trace every exported factual statement and question premise to the frozen source text; changing evidence forces re-review. Restart during either review gate retains decisions; repeated resume/export requests create no duplicate revisions or artifacts.

Hard gates: all exported references pass deterministic validation; no cross-matter reference resolves; no rejected/stale item appears as accepted content; no external network/model download occurs in tests. Evaluate extraction coverage, reviewer corrections, event-date accuracy, finding precision, question usefulness, run cost, and latency separately on reviewed synthetic examples. Do not claim retrieval or investigation quality from the 14-case smoke fixture or the Citation-Recovery Benchmark, which retains its existing protocol and meaning.

## Deferred work

Multi-tenant hosting, distributed workers, external vector databases, graph databases, autonomous web investigation, authority weights, treatment inference, credibility scores, legal-outcome prediction, comprehensive discovery management, and automated document correction are outside V1. This planning change adds no runtime storage or dependencies.

# Precedent Trace + Authority Intelligence + Doctrine Evolution v1

This is an inspectable product view over the existing **local** LexTrace corpus
and immutable citation graph, not a complete citator or a legal-outcome model.
The Matter claim, normalized proposition, forum, and optional as-of date select
a small earlier/later neighborhood. Defaults are one hop each, 20 nodes, and
30 edges; API bounds are two hops, 50 nodes, and 100 edges. Graph links and
proposition-matching local passages are shown as separate diagnostics.

## Court relationship

Known federal courts are centralized in `graph/courts.py`. For a federal
district Matter, the Supreme Court and its own circuit are classified
`CONTROLLING` as **hierarchy context**, the Matter's district is `SAME_COURT`,
and other districts/circuits are `PERSUASIVE`. Missing or unmapped forum/court
metadata yields `UNKNOWN`. These categories do not establish publication,
precedential status, jurisdictional exceptions, temporal validity, or issue fit.
State hierarchy is deferred.

## Context and treatment

For a directed case citation edge, LexTrace searches the graph-provenance
citing opinion for a unique exact reporter citation or case name belonging to
the cited local Case. A matched source passage retains opinion ID, stable
passage ID, character offsets, exact text, and CourtListener URL. No match
means context unavailable. Explicit nearby active treatment verbs can produce
`FOLLOWS`, `APPLIES`, `RELIES_ON`, `DISTINGUISHES`, `LIMITS`, `CRITICIZES`, or
`OVERRULES`. Negation and passive “overruled by” block automatic assignment.
Otherwise a recovered context is `CITES`, and an unrecovered one is `UNKNOWN`.
All specific labels require review; `OVERRULES` is never silently confirmed.
Generated label, method, evidence, and later human review state are stored
separately from raw `CitationEdge`. A reviewer may confirm, reject, or mark
uncertain; rejection excludes an event from the derived timeline.

## Timeline and impact

Only a specific, evidence-bearing treatment annotation with local
proposition-matching text becomes a dated `DoctrineEvent`. The UI calls this
**relevant retrieved doctrinal history**. It is neither exhaustive nor a
verified treatment history. Every displayed synthesis statement references an
annotation and exact citing-opinion passage. A deterministic impact category
distinguishes supportive, limiting, mixed, and insufficient local evidence;
only human-confirmed later treatment changes the argument-impact category.
It does not predict an outcome. No LLM is used for treatment or synthesis v1.

Claim-level doctrine responses are cached in the private Matter SQLite
database using proposition, graph ID, corpus hash, passage settings, forum,
date, limits, and rule versions. Claim edits and human review invalidate that
claim's derived cache. APIs expose bounded trace, authority analysis, doctrine,
and treatment review under `/matters/{matter_id}/claims/{claim_id}/`; a public
case trace is available at `/cases/{case_id}/precedent-trace`.

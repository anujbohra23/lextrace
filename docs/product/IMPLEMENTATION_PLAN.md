# LexTrace Investigations — V1 Implementation Plan

Status: proposed development sequence. This change creates planning documents only; no application code, dependency installation, data migration, commit or push is included. Follow [PRODUCT_SPEC.md](PRODUCT_SPEC.md) and [ARCHITECTURE.md](ARCHITECTURE.md).

## Delivery rules

Keep each milestone reviewable and independently testable. Preserve search, research, Argument X-Ray, citation intelligence, monitoring, ingestion, benchmarks and CLI behavior. Use Python 3.12 and the existing environment; propose any new dependency explicitly in the relevant coding change. Do not add persistent workflow storage until its scoped milestone. Keep generated data private and ignored, with small redistributable synthetic fixtures in tests/fixtures.

For backend changes run pytest, ruff check ., ruff format --check ., and mypy with existing configured tools. For frontend changes also run npm test, npm run lint, npm run typecheck, and npm run build in frontend. Do not download models or call providers in tests. If an environment prerequisite prevents a check, report it precisely rather than treating the check as passed. Documentation-only planning does not establish a passing application baseline; tests were inspected, not executed for this change.

## Milestones

| Milestone | Focused deliverable | Observable acceptance tests / exit gate | Dependencies |
| --- | --- | --- | --- |
| M1 — Source reference contract | Add immutable investigation document-version and evidence-reference contracts plus pure source validator; no persistence or API changes | Exact Unicode slice succeeds; wrong hash/version/document/matter, out-of-bounds, empty/reversed span and quote mismatch fail; unchanged old matter contracts | None |
| M2 — Versioned storage | Add explicit migrations, investigation repository, extraction manifests, ownership keys and private-artifact lifecycle; reuse uploads | Upgrade a populated legacy DB without losing research/monitoring data; identical bytes in two matters do not collide; concurrent writes safe; deletion removes versions/index/checkpoints/exports and guards active jobs | M1 |
| M3 — Factual extraction | Add entity mentions, attributed assertions, event and temporal schemas; bounded extraction/verification services using StructuredLLM | Fake outputs produce exact source-backed records; repeated quotes are disambiguated by position; malformed/hallucinated sources quarantined; coverage reports skipped windows/pages; OCR_REQUIRED excluded visibly | M2 |
| M4 — Reviewable chronology | Add deterministic chronology builder and versioned entity/date review with targeted invalidation | Unknown, partial, relative and overlapping dates remain explicit; reviewer identity merge and date edit create revisions; unchanged assertions retain identity; stale downstream data invalidates | M3 |
| M5 — Private hybrid retrieval | Extract minimal shared BM25 ranking core with public parity tests; add private passage adapters using existing dense/RRF/scorer interfaces | Existing case rankings, numeric ties and contract unchanged; private matter/version filters precede ranking; overlap deduplication and exact offsets hold; index identity rejects changed settings; fake model tests only | M2; before M6 |
| M6 — Inconsistency investigation | Add bounded pair selection, contextual evidence search, structured hypotheses, alternatives and independent validation | Two conflicting accounts yield a sourced potential finding; same event ambiguity, different time scopes, negation, hearsay and missing evidence do not become categorical contradictions; pair/search budgets enforced | M3–M5 |
| M7 — Durable LangGraph review | Compose nodes and tools, injected saver, durable local checkpoint integration, two human gates, idempotent runtime and cancel/recovery | Restart at review gate retains state; repeated/concurrent resume cannot duplicate decisions/work; crash after decision commit recovers; wrong thread/matter and stale digest rejected; budget exhaustion and cancellation stop publication | M4, M6 |
| M8 — Investigation API and UI | New thin router, polling and Investigation/Chronology/Review panels in existing matter workspace | TestClient and component tests cover upload-to-review, side-by-side sources, stale-edit conflict, invalid refs, partial coverage and Unicode highlighting; old routes/panels still work | M7 |
| M9 — Witness pack draft | Add witness profile, topic/question contracts, approved-snapshot drafting and premise verification; pack preview/review | Only accepted current evidence appears; unknown witness identity and unsupported premises block assertions; neutral questions permitted; source changes force re-review; empty evidence cannot generate fictional background | M7; integrate UI after M8 |
| M10 — DOCX export | Select a maintained writer in the coding change, add deterministic template and approved export service/download | Reject unapproved/stale pack; repeated exports reuse complete artifact; injected render failure publishes nothing; DOCX contains chronology/questions/source index and readable exact locators; verify rendered pages for tables, long filenames and page breaks | M9 |
| M11 — Pilot hardening | Full synthetic workflow, regression suite, restart/cancel/delete cases and reviewed usefulness assessment | Complete Create Matter → DOCX with both review gates and no network; audit every factual premise/source reference; report correction rates and finding precision independently from retrieval benchmark metrics | M8–M10 |

M2 storage work must settle connection ownership and legacy section-key handling before M3 relies on document versions. M5 can follow the contract/storage slice without waiting for the UI. M7 must prove real checkpoint continuation; a RunStore-based full restart is not an acceptable substitute. M10 must include artifact visual inspection using the applicable document tooling when implementing it.

## Suggested small change boundaries

Within M2, separate migrations/repository from upload integration and lifecycle cleanup. Within M5, land public BM25 parity-preserving extraction before the evidence retrieval adapter. Within M7, separate graph behavior with an in-memory saver from durable saver and crash recovery integration. Within M8, land typed API tests before UI panels. Within M9, land pack contracts/validator before model drafting. Within M10, land template/renderer before export endpoints. Keep each boundary green and avoid a broad rewrite of api/app.py or matter/store.py.

## Exact first coding task

Implement only the immutable investigation evidence-reference slice (M1).

Proposed files:

- `src/lextrace/investigations/__init__.py`: package marker.
- `src/lextrace/investigations/contracts.py`: `DocumentVersion` and `EvidenceReference` using strict existing Record conventions. Include matter/document/version IDs, original and extracted hashes, extraction identity, text length, and reference section/offset/quote fields. Optional page metadata must not imply DOCX pagination.
- `src/lextrace/investigations/provenance.py`: pure `validate_evidence_reference(reference, version, section, extracted_text)` returning typed validation issues. Validate ownership, version/hash/length, section containment, positive ordered bounds and exact Python Unicode slice equality. It must reject invalid provenance regardless of a model's opinion.
- `tests/unit/test_investigation_provenance.py`: focused tests with in-memory synthetic text and existing DocumentSection/SourceSpan fixtures.

Test the valid reference, quote mismatch, wrong matter/document/version, bad extracted hash, malformed bounds, a section from another document, repeated quote at a wrong position, and non-BMP characters. Repeated identical quotes at valid different positions are valid distinct spans; the validator does not guess the intended occurrence. Test reference immutability and forbidden extra fields. No LLM, database, upload route, frontend, LangGraph, new dependency or Case/MatterEvidence changes belong in this first task.

Done means the targeted tests and all repository backend checks pass, existing source-span tests still pass, and the diff is limited to this contract/validator slice. This gives every later agent and review step one testable grounding boundary.

## Decisions to validate during implementation

- M2: migration compatibility with existing runtime databases and a composite section ownership strategy; never mutate old provenance silently.
- M3/M6: tune proposed corpus/window/search limits on synthetic data and record coverage; no capacity or precision claims before measurement.
- M5: minimal generic ranking interface that keeps a single BM25 implementation and public ranking parity.
- M7: verify installed LangGraph API and supported local durable saver dependency; document schema upgrades, replay and checkpoint cleanup. No installed saver was assumed in this planning pass.
- M9: whether optional legal context is needed for the pilot; default factual preparation works without it or a loaded public corpus.
- M10: DOCX library/template choice, portable source-key links and rendering verification approach. Input DOCX parsing does not provide output generation.
- Before any shared deployment: explicitly scope authentication, authorization, encryption/key handling, backups, retention and provider terms. V1 remains a trusted local pilot until that work is complete.

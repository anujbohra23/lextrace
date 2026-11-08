"""Offline M6B: real private storage/review/search, injectable structured models."""

from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal, cast

import pytest
from pydantic import ValidationError

from lextrace.investigations.contracts import (
    AttributedAssertion,
    EntityMention,
    EvidenceReference,
    SourceIdentity,
    TemporalReference,
)
from lextrace.investigations.evidence_search import (
    ApprovedVersion,
    EvidenceSearchRequest,
    EvidenceSearchResult,
    EvidenceSearchService,
)
from lextrace.investigations.findings import (
    ALL_DIMENSIONS,
    ALL_EXPLANATIONS,
    AccountPremise,
    AlternativeExplanation,
    ContextCheck,
    EvidenceAssessment,
    Hypothesis,
    InvestigationBudgets,
    InvestigationResult,
)
from lextrace.investigations.inconsistency_candidates import (
    CandidateInput,
    CandidatePair,
)
from lextrace.investigations.inconsistency_investigation import (
    InconsistencyInvestigationService,
    evidence_id,
)
from lextrace.investigations.repository import InvestigationRepository
from lextrace.investigations.review import target_of
from lextrace.matter.store import MatterStore
from lextrace.research.contracts import ResearchError, Usage
from lextrace.research.llm import Output
from lextrace.research.prompts import PromptDefinition
from tests.unit.test_investigation_review import command


class Model:
    provider = "fake"
    model = "offline"
    retries = 0
    usage = Usage()

    def __init__(self) -> None:
        self.calls = 0
        self.hook: Callable[[], None] = lambda: None
        self.mutate: Callable[[Hypothesis], Hypothesis] = lambda h: h

    def generate(
        self, prompt: PromptDefinition, schema: type[Output], context: dict[str, object]
    ) -> Output:
        self.calls += 1
        self.hook()
        refs = cast(dict[str, dict[str, object]], context["evidence"])
        accounts = cast(list[dict[str, object]], context["accounts"])
        sides: dict[str, str] = {}
        for side, account in zip(("left", "right"), accounts, strict=True):
            original = cast(dict[str, object], account["original"])
            source = cast(list[dict[str, object]], original["evidence_references"])[0]
            sides[side] = evidence_id(EvidenceReference.model_validate(source))
        keys = tuple(sorted(refs))
        hypothesis = Hypothesis(
            status="POTENTIAL_INCONSISTENCY",
            premises=tuple(
                AccountPremise(
                    side=cast(Literal["left", "right"], side),
                    dimension=d,
                    entailment="SUPPORTED",
                    evidence_ids=(key,),
                )
                for side, key in sides.items()
                for d in ALL_DIMENSIONS
            ),
            checks=tuple(
                ContextCheck(dimension=d, assessment="COMPARABLE", evidence_ids=keys)
                for d in ALL_DIMENSIONS
            ),
            alternatives=tuple(
                AlternativeExplanation(
                    explanation=e,
                    assessment="NOT_SUPPORTED",
                    evidence_ids=keys,
                )
                for e in ALL_EXPLANATIONS
            ),
            evidence=tuple(
                EvidenceAssessment(
                    evidence_id=k,
                    role="SUPPORTING"
                    if k == sides["left"]
                    else "CONTRARY"
                    if k == sides["right"]
                    else "CONTEXT",
                    entailment="SUPPORTED",
                )
                for k in keys
            ),
        )
        return schema.model_validate(self.mutate(hypothesis).model_dump(), strict=True)


class Search:
    def __init__(self, inner: EvidenceSearchService) -> None:
        self.inner = inner
        self.calls = 0
        self.hook: Callable[[], None] = lambda: None
        self.mutate: Callable[[EvidenceSearchResult], EvidenceSearchResult] = (
            lambda r: r
        )

    def search(self, request: EvidenceSearchRequest) -> EvidenceSearchResult:
        self.calls += 1
        result = self.inner.search(request)
        self.hook()
        return self.mutate(result)


class Runtime:
    def __init__(self, store: MatterStore, repo: InvestigationRepository) -> None:
        self.store, self.repo = store, repo
        self.matter = store.create_matter("Synthetic").matter_id
        self.reasoner, self.verifier = Model(), Model()
        self.search = Search(EvidenceSearchService(store, repo))
        self.service = InconsistencyInvestigationService(
            store,
            repo,
            self.search,
            self.reasoner,
            self.verifier,
        )
        self.approved: list[ApprovedVersion] = []

    def account(self, identity: str, **changes: object) -> CandidateInput:
        text = (
            "Alice: I delivered the package to Bob on 2020-01-01."
            if identity == "a"
            else "Alice: I did not deliver the package to Bob on 2020-01-01."
        )
        doc = self.store.add_document(self.matter, identity + ".txt", text.encode())
        version = self.repo.active_document_version(self.matter, doc.document_id)
        assert version is not None
        self.approved.append(
            ApprovedVersion(document_id=doc.document_id, version_id=version.version_id)
        )
        section = self.store.sections(self.matter, doc.document_id)[0]
        base = dict(
            matter_id=self.matter,
            document_id=doc.document_id,
            version_id=version.version_id,
            run_id="synthetic",
            created_at=datetime(2020, 1, 1, tzinfo=UTC),
            generator="synthetic",
        )
        ref = EvidenceReference(
            **{k: getattr(version, k) for k in SourceIdentity.model_fields},
            section_id=section.section_id,
            start=0,
            end=len(text),
            exact_quote=text,
        )
        temporal = TemporalReference.model_validate(
            dict(
                **base,
                temporal_id=identity,
                kind="exact",
                raw_expression="2020-01-01",
                earliest=date(2020, 1, 1),
                latest=date(2020, 1, 1),
                precision="day",
                evidence_references=(ref,),
                verification_state="VALID",
            )
        )
        if changes.pop("partial_time", False):
            temporal = temporal.model_copy(
                update={
                    "kind": "approximate",
                    "raw_expression": "2020-01",
                    "earliest": None,
                    "latest": None,
                    "precision": "month",
                }
            )
        if changes.pop("unknown_time", False):
            temporal = temporal.model_copy(
                update={
                    "kind": "unknown",
                    "raw_expression": None,
                    "earliest": None,
                    "latest": None,
                    "precision": "unknown",
                }
            )
        assertion = AttributedAssertion.model_validate(
            dict(
                **base,
                assertion_id=identity,
                normalized_proposition=text,
                attributed_to="Alice",
                attribution_status="known",
                subject="Alice",
                predicate="delivered",
                object="package",
                modality="asserted",
                polarity="positive" if identity == "a" else "negative",
                stated_time=temporal,
                evidence_references=(ref,),
                verification_state="VALID",
            )
            | changes
        )
        self.repo.register_review_candidate(assertion)
        reviewed = self.repo.review(
            command(assertion, decision_id="assert-" + identity)
        )
        mention_ref = ref.model_copy(update={"end": 5, "exact_quote": "Alice"})
        mention = EntityMention.model_validate(
            dict(
                **base,
                mention_id=identity,
                entity_type="person",
                mention_text="Alice",
                evidence_references=(mention_ref,),
            )
        )
        self.repo.register_review_candidate(mention)
        self.repo.review(
            command(
                mention,
                decision="EDIT",
                resolved_entity_id="alice",
                decision_id="resolve-" + identity,
            )
        )
        resolved = self.repo.review(
            command(mention, expected_revision=2, decision_id="accepted-" + identity)
        )
        return CandidateInput(revision=reviewed, entity_mentions=(resolved,))

    def pair(self, **changes: object) -> CandidatePair:
        a, b = self.account("a"), self.account("b", **changes)
        # M6A helper uses a fixed matter; call the pure selector with real ownership.
        from dataclasses import replace

        from lextrace.investigations.inconsistency_candidates import (
            select_inconsistency_candidates,
        )
        from lextrace.investigations.review import RecordDependency
        from tests.unit.test_investigation_chronology import result

        revisions = (a.revision, b.revision, *a.entity_mentions, *b.entity_mentions)
        sources = tuple(
            SourceIdentity.model_validate(
                {
                    k: getattr(r.original.evidence_references[0], k)
                    for k in SourceIdentity.model_fields
                }
            )
            for r in revisions
        )
        selection = select_inconsistency_candidates(
            (a, b),
            matter_id=self.matter,
            sources=sources,
            verified=(
                replace(
                    result(), valid_candidates=tuple(r.original for r in revisions)
                ),
            ),
            current=tuple(
                RecordDependency(target=target_of(r.original), revision=r.revision)
                for r in revisions
            ),
        )
        return selection.pairs[0]

    def run(
        self, *pairs: CandidatePair, budgets: InvestigationBudgets | None = None
    ) -> InvestigationResult:
        return self.service.investigate(
            tuple(pairs),
            matter_id=self.matter,
            approved_versions=tuple(self.approved),
            budgets=budgets,
        )


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[Runtime]:
    store = MatterStore(tmp_path / "db", tmp_path / "private")
    repo = InvestigationRepository(tmp_path / "db", source_store=store)
    yield Runtime(store, repo)
    repo.close()
    store.close()


def test_potential_exact_sources_and_deterministic_duplicates(runtime: Runtime) -> None:
    pair = runtime.pair()
    first = runtime.run(pair, pair)
    assert len(first.findings) == 1
    assert first.findings[0].status == "POTENTIAL_INCONSISTENCY"
    assert first.findings[0].verification == "INDEPENDENT_STRUCTURED_SUPPORT"
    assert first.findings[0].dependencies == pair.dependencies
    assert first.searches == 1 and first.model_calls == 2
    assert first == runtime.run(pair)
    with pytest.raises(ValidationError):
        first.findings[0].status = "NO_ESTABLISHED_CONFLICT"


@pytest.mark.parametrize("dimension", ["recipient", "event_scope", "time", "identity"])
def test_incomparable_context_is_insufficient(runtime: Runtime, dimension: str) -> None:
    pair = runtime.pair()

    def change(h: Hypothesis) -> Hypothesis:
        return h.model_copy(
            update={
                "checks": tuple(
                    c.model_copy(update={"assessment": "DIFFERENT"})
                    if c.dimension == dimension
                    else c
                    for c in h.checks
                )
            }
        )

    runtime.reasoner.mutate = runtime.verifier.mutate = change
    assert runtime.run(pair).findings[0].status == "INSUFFICIENT_EVIDENCE"


@pytest.mark.parametrize(
    "change",
    [
        {"modality": "alleged"},
        {"modality": "conditional"},
        {"stated_time": None},
        {"polarity": "positive"},
    ],
)
def test_modality_missing_time_and_paraphrase(
    runtime: Runtime, change: dict[str, object]
) -> None:
    pair = runtime.pair(**change)
    assert runtime.run(pair).findings[0].status == "INSUFFICIENT_EVIDENCE"


@pytest.mark.parametrize("kind", ["unknown", "approximate", "interval"])
def test_partial_or_unverified_dates(runtime: Runtime, kind: str) -> None:
    # Unverified bounded partial dates are rejected by reused M3 verification.
    pair = runtime.pair()
    original = pair.right.revision.original
    assert (
        isinstance(original, AttributedAssertion) and original.stated_time is not None
    )
    temporal = original.stated_time.model_copy(
        update={
            "kind": kind,
            "earliest": None if kind == "unknown" else date(2020, 1, 1),
            "latest": None if kind == "unknown" else date(2020, 1, 31),
            "precision": "unknown" if kind == "unknown" else "month",
        }
    )
    # Caller-forged replacement cannot stand in for the stored immutable revision.
    forged = pair.model_copy(
        update={
            "right": pair.right.model_copy(
                update={
                    "revision": pair.right.revision.model_copy(
                        update={
                            "original": original.model_copy(
                                update={"stated_time": temporal}
                            )
                        }
                    )
                }
            )
        }
    )
    assert not runtime.run(forged).findings
    assert runtime.reasoner.calls == 0


@pytest.mark.parametrize("fault", ["id", "offset", "quote", "section", "matter"])
def test_invented_evidence_rejected(runtime: Runtime, fault: str) -> None:
    pair = runtime.pair()
    if fault == "id":
        runtime.reasoner.mutate = lambda h: h.model_copy(
            update={
                "checks": (
                    h.checks[0].model_copy(update={"evidence_ids": ("f" * 64,)}),
                    *h.checks[1:],
                )
            }
        )
    else:

        def corrupt(r: EvidenceSearchResult) -> EvidenceSearchResult:
            hit = r.hits[0]
            corruptions: dict[str, dict[str, object]] = {
                "offset": {"end": hit.reference.end + 1},
                "quote": {"exact_quote": "hallucinated"},
                "section": {"section_id": "wrong"},
                "matter": {"matter_id": "wrong"},
            }
            changes = corruptions[fault]
            return r.model_copy(
                update={
                    "hits": (
                        hit.model_copy(
                            update={
                                "reference": hit.reference.model_copy(update=changes)
                            }
                        ),
                    )
                }
            )

        runtime.search.mutate = corrupt
    assert not runtime.run(pair).findings
    assert runtime.verifier.calls == 0


@pytest.mark.parametrize("code", ["UNSUPPORTED", "UNKNOWN"])
def test_independent_semantic_rejection(runtime: Runtime, code: str) -> None:
    pair = runtime.pair()
    runtime.verifier.mutate = lambda h: h.model_copy(
        update={
            "premises": (
                h.premises[0].model_copy(update={"entailment": code}),
                *h.premises[1:],
            )
        }
    )
    finding = runtime.run(pair).findings[0]
    assert finding.status == "INSUFFICIENT_EVIDENCE"
    assert finding.verification == "INSUFFICIENT"


@pytest.mark.parametrize("boundary", ["before", "search", "reasoner", "verifier"])
@pytest.mark.parametrize("mutation", ["review", "delete", "version"])
def test_currency_changes_fail_closed(
    runtime: Runtime, boundary: str, mutation: str
) -> None:
    pair = runtime.pair()
    original = pair.left.revision.original

    def change() -> None:
        if mutation == "review":
            runtime.repo.review(
                command(
                    original,
                    expected_revision=2,
                    decision_id="changed",
                    decision="REJECT",
                )
            )
        elif mutation == "delete":
            runtime.store.delete_document(runtime.matter, original.document_id)
        else:
            version = runtime.repo.active_document_version(
                runtime.matter, original.document_id
            )
            assert version is not None
            runtime.repo.create_document_version(
                version.model_copy(update={"version_id": "new"})
            )

    if boundary == "before":
        change()
    else:
        getattr(runtime, boundary).hook = change
    assert not runtime.run(pair).findings


@pytest.mark.parametrize(
    "budget",
    [
        {"max_pairs": 0},
        {"max_searches": 0},
        {"max_model_calls": 1},
        {"max_source_characters": 0},
        {"max_input_characters": 0},
        {"max_alternatives": 6},
        {"searches_per_pair": 0},
    ],
)
def test_budget_before_expensive_calls(
    runtime: Runtime, budget: dict[str, int]
) -> None:
    pair = runtime.pair()
    output = runtime.run(pair, budgets=InvestigationBudgets.model_validate(budget))
    assert not output.findings and output.partial
    assert runtime.search.calls == runtime.reasoner.calls == runtime.verifier.calls == 0


def test_model_and_search_failure_safe(runtime: Runtime) -> None:
    pair = runtime.pair()

    def fail() -> None:
        raise ResearchError("PRIVATE SECRET", category="provider_error")

    runtime.reasoner.hook = fail
    output = runtime.run(pair)
    assert not output.findings and "PRIVATE" not in output.model_dump_json()
    runtime.search.hook = fail
    assert not runtime.run(pair).findings


@pytest.mark.parametrize("change", [{"partial_time": True}, {"unknown_time": True}])
def test_trusted_partial_dates_remain_insufficient(
    runtime: Runtime, change: dict[str, bool]
) -> None:
    pair = runtime.pair(**change)
    result = runtime.run(pair)
    assert result.findings[0].status == "INSUFFICIENT_EVIDENCE"
    assert result.findings[0].verification == "INSUFFICIENT"


@pytest.mark.parametrize(
    "explanation",
    ["HEARSAY", "SAME_EVENT_PARAPHRASE", "OVERLAPPING_DATES", "UNCLEAR_CHRONOLOGY"],
)
def test_competing_explanations(runtime: Runtime, explanation: str) -> None:
    pair = runtime.pair()

    def change(h: Hypothesis) -> Hypothesis:
        return h.model_copy(
            update={
                "alternatives": tuple(
                    a.model_copy(update={"assessment": "POSSIBLE"})
                    if a.explanation == explanation
                    else a
                    for a in h.alternatives
                )
            }
        )

    runtime.reasoner.mutate = runtime.verifier.mutate = change
    assert runtime.run(pair).findings[0].status == "INSUFFICIENT_EVIDENCE"


def test_missing_contrary_support(runtime: Runtime) -> None:
    pair = runtime.pair()

    def change(h: Hypothesis) -> Hypothesis:
        return h.model_copy(
            update={
                "evidence": tuple(
                    e.model_copy(update={"role": "CONTEXT"})
                    if e.role == "CONTRARY"
                    else e
                    for e in h.evidence
                )
            }
        )

    runtime.reasoner.mutate = runtime.verifier.mutate = change
    assert runtime.run(pair).findings[0].status == "INSUFFICIENT_EVIDENCE"


def test_no_conflict_and_empty_retrieval(runtime: Runtime) -> None:
    pair = runtime.pair(polarity="positive")
    runtime.search.mutate = lambda r: r.model_copy(update={"hits": ()})

    def change(h: Hypothesis) -> Hypothesis:
        return h.model_copy(update={"status": "NO_ESTABLISHED_CONFLICT"})

    runtime.reasoner.mutate = runtime.verifier.mutate = change
    finding = runtime.run(pair).findings[0]
    assert finding.status == "NO_ESTABLISHED_CONFLICT"
    assert finding.accounts  # Only the exact upstream source quotes remain.


@pytest.mark.parametrize("state", ["REJECT", "DEFER", "EDIT"])
def test_unaccepted_current_review_excluded(runtime: Runtime, state: str) -> None:
    pair = runtime.pair()
    original = pair.right.revision.original
    kwargs: dict[str, object] = dict(
        decision=state, decision_id="new", expected_revision=2
    )
    if state == "EDIT":
        from lextrace.investigations.review import DateCorrection

        kwargs["date_correction"] = DateCorrection(kind="unknown")
    current = runtime.repo.review(command(original, **kwargs))
    from hashlib import sha256

    from lextrace.investigations.review import RecordDependency

    # Recreate a structurally sound M6A identity with the non-accepted revision.
    right = pair.right.model_copy(update={"revision": current})
    deps = tuple(
        sorted(
            (
                RecordDependency(target=target_of(r.original), revision=r.revision)
                for i in (pair.left, right)
                for r in (i.revision, *i.entity_mentions)
            ),
            key=lambda d: d.model_dump_json(),
        )
    )

    def key(i: CandidateInput) -> str:
        return RecordDependency(
            target=target_of(i.revision.original), revision=i.revision.revision
        ).model_dump_json()

    digest = sha256(
        (
            "deterministic-opportunities-v1"
            + "".join(d.model_dump_json() for d in deps)
            + key(pair.left)
            + key(right)
        ).encode()
    ).hexdigest()
    modified = pair.model_copy(
        update={"right": right, "dependencies": deps, "pair_id": digest}
    )
    assert not runtime.run(modified).findings
    assert runtime.search.calls == 0


def test_wrong_matter_scope(runtime: Runtime) -> None:
    pair = runtime.pair()
    other = runtime.store.create_matter("Other")
    result = runtime.service.investigate(
        (pair,), matter_id=other.matter_id, approved_versions=tuple(runtime.approved)
    )
    assert not result.findings and result.partial
    assert runtime.search.calls == 0


def test_malformed_output_and_no_free_prose(runtime: Runtime) -> None:
    pair = runtime.pair()
    runtime.reasoner.mutate = lambda h: h.model_copy(update={"status": "GUILTY"})
    result = runtime.run(pair)
    assert not result.findings and result.diagnostics[0].code == "MODEL_FAILED"
    assert "GUILTY" not in result.model_dump_json()


def test_model_failure_identifies_stage_and_safe_category(runtime: Runtime) -> None:
    pair = runtime.pair()

    def fail() -> None:
        raise ResearchError("private provider detail", category="provider_timeout")

    runtime.reasoner.hook = fail
    result = runtime.run(pair)
    detail = next(d for d in result.diagnostics if d.stage == "reasoner")
    assert detail.code == "MODEL_FAILED" and detail.reason == "provider_timeout"
    assert "private provider detail" not in result.model_dump_json()


def test_unexpected_programming_errors_are_not_model_failures(runtime: Runtime) -> None:
    pair = runtime.pair()

    def bug() -> None:
        raise RuntimeError("programming defect")

    runtime.reasoner.hook = bug
    with pytest.raises(RuntimeError, match="programming defect"):
        runtime.run(pair)


def test_publication_boundary_rechecks_all_reviews(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    pair = runtime.pair()
    original = pair.left.revision.original
    read = runtime.repo.current_review
    reads = 0
    active = False

    def activate() -> None:
        nonlocal active
        active = True

    def checked(target: object) -> object:
        nonlocal reads
        if active:
            reads += 1
            if reads == 5:  # Four post-model reads, then publication reads begin.
                runtime.repo.review(
                    command(
                        original,
                        expected_revision=2,
                        decision_id="publication-change",
                        decision="REJECT",
                    )
                )
        return read(target)  # type: ignore[arg-type]

    runtime.verifier.hook = activate
    monkeypatch.setattr(runtime.repo, "current_review", checked)
    result = runtime.run(pair)
    assert not result.findings
    assert any(d.code == "CURRENCY_CHANGED" for d in result.diagnostics)

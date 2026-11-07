"""Bounded investigative opportunities, never verified inconsistency findings.

Trusted callers supply fresh M3 verification results, current M4 dependencies and
active immutable source identities. This pure boundary cannot discover repository
currency. Recheck these inputs before downstream publication. Entity links are
explicit caller-supplied links to reviewed mentions, never inferred from names.
"""

from hashlib import sha256
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from lextrace.investigations.contracts import (
    SHA256,
    AttributedAssertion,
    EntityMention,
    InvestigationEvent,
    SourceIdentity,
)
from lextrace.investigations.extraction_verification import ExtractionVerificationResult
from lextrace.investigations.review import (
    RecordDependency,
    ReviewContract,
    ReviewRevision,
    target_of,
)

Positive = Annotated[int, Field(gt=0)]
Code = Literal[
    "MATCHED_REVIEWED_ENTITY",
    "EXACT_DESCRIPTOR",
    "OPPOSING_POLARITY",
    "EXACT_DATE_DIFFERENCE",
    "OVERLAPPING_DATES",
    "WITHIN_DATE_WINDOW",
    "INSUFFICIENT_TIME",
    "AMBIGUOUS_TIME",
    "SAME_SPEAKER",
    "DIFFERENT_SPEAKERS",
    "UNKNOWN_ATTRIBUTION",
    "NON_ASSERTED_MODALITY",
    "EVENT_SCOPE_REQUIRES_REVIEW",
    "DIFFERENT_OBJECTS",
    "INSUFFICIENT_DESCRIPTOR",
    "NO_SHARED_REVIEWED_ENTITY",
    "DIFFERENT_DESCRIPTOR",
    "OUTSIDE_DATE_WINDOW",
    "INELIGIBLE_INPUT",
    "INPUT_BUDGET",
    "COMPARISON_BUDGET",
    "PAIR_BUDGET",
    "SOURCE_BUDGET",
    "TARGET_BUDGET",
]


class CandidateInput(ReviewContract):
    revision: ReviewRevision
    # Each mention is an explicit subject/participant link, not a speaker inference.
    entity_mentions: tuple[ReviewRevision, ...] = ()


class CandidateBudgets(ReviewContract):
    max_inputs: Positive = 100
    max_mentions_per_input: Positive = 20
    max_comparisons: Positive = 1000
    max_pairs: Positive = 200
    max_pairs_per_source: Positive = 40
    max_pairs_per_target: Positive = 20
    date_window_days: Annotated[int, Field(ge=0)] = 30


class CandidatePair(ReviewContract):
    pair_id: SHA256
    left: CandidateInput
    right: CandidateInput
    dependencies: tuple[RecordDependency, ...]
    reasons: tuple[Code, ...]
    cautions: tuple[Code, ...]
    status: Literal["CANDIDATE_FOR_REVIEW"] = "CANDIDATE_FOR_REVIEW"
    # Source and nested temporal evidence live unchanged in the original revisions.

    @model_validator(mode="after")
    def valid_pair(self) -> Self:
        if _key(self.left) >= _key(self.right):
            raise ValueError("Pair endpoints must be distinct and canonically ordered.")
        if (
            self.left.revision.original.matter_id
            != self.right.revision.original.matter_id
        ):
            raise ValueError("Cross-matter pair.")
        deps = {
            _dependency(r).model_dump_json(): _dependency(r)
            for i in (self.left, self.right)
            for r in (i.revision, *i.entity_mentions)
        }
        expected = tuple(deps[k] for k in sorted(deps))
        digest = sha256(
            (
                "deterministic-opportunities-v1"
                + "".join(d.model_dump_json() for d in expected)
                + _key(self.left)
                + _key(self.right)
            ).encode()
        ).hexdigest()
        if self.dependencies != expected or self.pair_id != digest or not self.reasons:
            raise ValueError("Invalid candidate pair identity or dependencies.")
        return self


class SelectionDiagnostic(ReviewContract):
    code: Code
    count: Positive


class CandidateSelection(ReviewContract):
    pairs: tuple[CandidatePair, ...]
    diagnostics: tuple[SelectionDiagnostic, ...]
    comparisons: Annotated[int, Field(ge=0)]
    partial: bool
    policy: Literal["deterministic-opportunities-v1"] = "deterministic-opportunities-v1"


def _dependency(revision: ReviewRevision) -> RecordDependency:
    return RecordDependency(
        target=target_of(revision.original), revision=revision.revision
    )


def _key(item: CandidateInput) -> str:
    return _dependency(item.revision).model_dump_json()


def select_inconsistency_candidates(
    inputs: tuple[CandidateInput, ...],
    *,
    matter_id: str,
    sources: tuple[SourceIdentity, ...],
    verified: tuple[ExtractionVerificationResult, ...],
    current: tuple[RecordDependency, ...],
    require_accepted: bool = True,
    budgets: CandidateBudgets | None = None,
) -> CandidateSelection:
    """Exact predicates/descriptions and reviewed identity links nominate pairs.

    Input overflow fails closed before sorting or quadratic work. Comparison and
    output caps truncate in canonical identity order, with visible partial coverage.
    Source budgets count document/version endpoints; target budgets count record
    endpoints, independent of left/right orientation. No lexical negation guessing,
    semantic similarity, credibility assessment, or assertion of contradiction.
    """
    if not matter_id.strip():
        raise ValueError("Matter identity is required.")
    budgets = CandidateBudgets.model_validate(
        (budgets or CandidateBudgets()).model_dump(), strict=True
    )
    counts: dict[Code, int] = {}
    partial = False

    def note(code: Code) -> None:
        counts[code] = counts.get(code, 0) + 1

    def finish(pairs: list[CandidatePair], comparisons: int) -> CandidateSelection:
        return CandidateSelection(
            pairs=tuple(pairs),
            comparisons=comparisons,
            partial=partial,
            diagnostics=tuple(
                SelectionDiagnostic(code=k, count=v) for k, v in sorted(counts.items())
            ),
        )

    if len(inputs) > budgets.max_inputs or any(
        len(i.entity_mentions) > budgets.max_mentions_per_input for i in inputs
    ):
        note("INPUT_BUDGET")
        partial = True
        return finish([], 0)
    current_map = {d.target.model_dump_json(): d.revision for d in current}
    if len(current_map) != len(current):
        raise ValueError("Duplicate current dependency.")
    source_set = {s.model_dump_json() for s in sources if s.matter_id == matter_id}
    trusted = {
        c.model_dump_json() for result in verified for c in result.valid_candidates
    }

    def eligible(r: ReviewRevision, *, mention: bool = False) -> bool:
        o = r.original
        if (
            o.matter_id != matter_id
            or current_map.get(target_of(o).model_dump_json()) != r.revision
        ):
            return False
        if o.model_dump_json() not in trusted:
            return False
        if r.review_state in ("REJECTED", "DEFERRED", "STALE") or (
            (require_accepted or mention) and r.review_state != "ACCEPTED"
        ):
            return False
        refs = list(o.evidence_references)
        if isinstance(o, AttributedAssertion | InvestigationEvent):
            t = o.temporal if isinstance(o, InvestigationEvent) else o.stated_time
            if o.verification_state != "VALID" or (
                t is not None and t.verification_state != "VALID"
            ):
                return False
            if t is not None:
                refs.extend(t.evidence_references)
        return all(
            SourceIdentity.model_validate(
                {k: getattr(ref, k) for k in SourceIdentity.model_fields}
            ).model_dump_json()
            in source_set
            for ref in refs
        )

    unique: dict[str, CandidateInput] = {}
    seen_revisions: dict[str, ReviewRevision] = {}
    for item in inputs:
        item = CandidateInput.model_validate(item.model_dump(), strict=True)
        mentions = tuple(
            sorted(
                set(item.entity_mentions),
                key=lambda r: _dependency(r).model_dump_json(),
            )
        )
        item = CandidateInput(revision=item.revision, entity_mentions=mentions)
        if not isinstance(
            item.revision.original, AttributedAssertion | InvestigationEvent
        ):
            raise ValueError("Expected assertion or event.")
        for revision in (item.revision, *item.entity_mentions):
            revision_key = _dependency(revision).model_dump_json()
            if (
                revision_key in seen_revisions
                and seen_revisions[revision_key] != revision
            ):
                raise ValueError("Conflicting upstream revision identity.")
            seen_revisions[revision_key] = revision
        key = _key(item)
        if key in unique and unique[key] != item:
            raise ValueError("Conflicting candidate identity.")
        unique[key] = item
    items: list[CandidateInput] = []
    for item in sorted(unique.values(), key=_key):
        if not eligible(item.revision) or any(
            not isinstance(m.original, EntityMention)
            or not eligible(m, mention=True)
            or m.resolved_entity_id is None
            for m in item.entity_mentions
        ):
            note("INELIGIBLE_INPUT")
        else:
            items.append(item)
    pairs: list[CandidatePair] = []
    source_counts: dict[tuple[str, str], int] = {}
    target_counts: dict[str, int] = {}
    comparisons = 0
    for index, left in enumerate(items):
        for right in items[index + 1 :]:
            if comparisons >= budgets.max_comparisons:
                note("COMPARISON_BUDGET")
                partial = True
                return finish(pairs, comparisons)
            comparisons += 1
            reasons, cautions = _signals(left, right, budgets.date_window_days)
            if not reasons:
                for caution in cautions:
                    note(caution)
                continue
            source_keys = {
                (i.revision.original.document_id, i.revision.original.version_id)
                for i in (left, right)
            }
            target_keys = {_key(left), _key(right)}
            if len(pairs) >= budgets.max_pairs:
                note("PAIR_BUDGET")
                partial = True
                return finish(pairs, comparisons)
            if any(
                source_counts.get(k, 0) >= budgets.max_pairs_per_source
                for k in source_keys
            ):
                note("SOURCE_BUDGET")
                partial = True
                continue
            if any(
                target_counts.get(k, 0) >= budgets.max_pairs_per_target
                for k in target_keys
            ):
                note("TARGET_BUDGET")
                partial = True
                continue
            deps = {
                _dependency(r).model_dump_json(): _dependency(r)
                for i in (left, right)
                for r in (i.revision, *i.entity_mentions)
            }
            dependencies = tuple(deps[k] for k in sorted(deps))
            digest = sha256(
                (
                    "deterministic-opportunities-v1"
                    + "".join(d.model_dump_json() for d in dependencies)
                    + _key(left)
                    + _key(right)
                ).encode()
            ).hexdigest()
            pairs.append(
                CandidatePair(
                    pair_id=digest,
                    left=left,
                    right=right,
                    dependencies=dependencies,
                    reasons=reasons,
                    cautions=cautions,
                )
            )
            for k in source_keys:
                source_counts[k] = source_counts.get(k, 0) + 1
            for target in target_keys:
                target_counts[target] = target_counts.get(target, 0) + 1
    return finish(pairs, comparisons)


def _signals(
    left: CandidateInput, right: CandidateInput, window: int
) -> tuple[tuple[Code, ...], tuple[Code, ...]]:
    a, b = left.revision.original, right.revision.original
    entities_a = {m.resolved_entity_id for m in left.entity_mentions}
    entities_b = {m.resolved_entity_id for m in right.entity_mentions}
    if not entities_a & entities_b:
        return (), ("NO_SHARED_REVIEWED_ENTITY",)
    da = (
        a.predicate
        if isinstance(a, AttributedAssertion)
        else getattr(a, "description", None)
    )
    db = (
        b.predicate
        if isinstance(b, AttributedAssertion)
        else getattr(b, "description", None)
    )
    if da is None or db is None:
        return (), ("INSUFFICIENT_DESCRIPTOR",)
    if da != db:
        return (), ("DIFFERENT_DESCRIPTOR",)
    reasons: list[Code] = ["MATCHED_REVIEWED_ENTITY", "EXACT_DESCRIPTOR"]
    cautions: list[Code] = ["EVENT_SCOPE_REQUIRES_REVIEW"]
    if isinstance(a, AttributedAssertion) and isinstance(b, AttributedAssertion):
        if {a.polarity, b.polarity} == {"positive", "negative"}:
            reasons.append("OPPOSING_POLARITY")
        if a.object != b.object:
            cautions.append("DIFFERENT_OBJECTS")
        if a.attribution_status != "known" or b.attribution_status != "known":
            cautions.append("UNKNOWN_ATTRIBUTION")
        else:
            cautions.append(
                "SAME_SPEAKER"
                if a.attributed_to == b.attributed_to
                else "DIFFERENT_SPEAKERS"
            )
        if a.modality != "asserted" or b.modality != "asserted":
            cautions.append("NON_ASSERTED_MODALITY")
    else:
        cautions.append("UNKNOWN_ATTRIBUTION")
    ta, tb = left.revision.interpreted_time(), right.revision.interpreted_time()
    if ta is None or tb is None or ta.earliest is None or tb.earliest is None:
        cautions.append("INSUFFICIENT_TIME")
    elif ta.kind != "exact" or tb.kind != "exact":
        cautions.append("AMBIGUOUS_TIME")
        assert ta.latest is not None and tb.latest is not None
        if ta.earliest <= tb.latest and tb.earliest <= ta.latest:
            reasons.append("OVERLAPPING_DATES")
    else:
        delta = abs((ta.earliest - tb.earliest).days)
        if delta > window:
            return (), ("OUTSIDE_DATE_WINDOW",)
        reasons.append("OVERLAPPING_DATES" if delta == 0 else "WITHIN_DATE_WINDOW")
        if delta:
            reasons.append("EXACT_DATE_DIFFERENCE")
    return tuple(reasons), tuple(cautions)

"""Bounded deterministic questions over accepted snapshot evidence, never names."""

from hashlib import sha256

from lextrace.deposition.contracts import (
    Category,
    EvidenceItem,
    PlanBasis,
    QuestionDraft,
    QuestionPolicy,
    WitnessDossier,
)
from lextrace.investigations.contracts import AttributedAssertion, InvestigationEvent
from lextrace.investigations.inconsistency_investigation import evidence_id
from lextrace.investigations.review import ReviewRevision, target_of
from lextrace.investigations.workflow import (
    ApprovedInvestigationSnapshot,
    InvestigationArtifacts,
)


def digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def generate_basis(
    snapshot: ApprovedInvestigationSnapshot,
    artifacts: InvestigationArtifacts,
    revisions: tuple[ReviewRevision, ...],
    plan_id: str,
) -> PlanBasis:
    """Caller independently validates currency, provenance and reviewed identities.

    M7 links are subject/participant links, NOT speaker associations. Dossiers are
    account-scoped even for identical attribution strings. No guessed alias merge.
    """
    limitations: list[str] = []
    coverage = tuple(v.coverage.status for v in artifacts.verified)
    if not coverage or any(c != "COMPLETE" for c in coverage):
        limitations.append("INCOMPLETE_COVERAGE")
    if any(
        v.quarantined_candidates or v.extraction_quarantine for v in artifacts.verified
    ):
        limitations.append("CANDIDATES_QUARANTINED")
    if any(v.coverage.issue_codes for v in artifacts.verified):
        limitations.append("COVERAGE_DIAGNOSTICS")
    if artifacts.result is not None:
        if artifacts.result.partial or artifacts.result.diagnostics:
            limitations.append("INVESTIGATION_LIMITED")
        if any(
            f.verification != "INDEPENDENT_STRUCTURED_SUPPORT"
            or f.status == "INSUFFICIENT_EVIDENCE"
            for f in artifacts.result.findings
        ):
            limitations.append("UNSUPPORTED_FINDINGS_EXCLUDED")
    witnesses: list[WitnessDossier] = []
    questions: list[QuestionDraft] = []
    policies: list[QuestionPolicy] = []
    registry: dict[str, EvidenceItem] = {}
    accepted = {
        target_of(r.original): r for r in revisions if r.review_state == "ACCEPTED"
    }
    attributable = [
        r
        for r in accepted.values()
        if isinstance(r.original, AttributedAssertion)
        and r.original.attribution_status == "known"
        and r.original.attributed_to
    ]
    if len(attributable) < sum(
        isinstance(r.original, AttributedAssertion) for r in accepted.values()
    ):
        limitations.append("UNATTRIBUTED_ACCOUNTS_EXCLUDED")
    if len(attributable) > 100:
        limitations.append("WITNESS_BUDGET")
    pairs = {p.pair_id: p for p in artifacts.pairs}

    for revision in sorted(
        attributable, key=lambda r: target_of(r.original).model_dump_json()
    )[:100]:
        original = revision.original
        assert isinstance(original, AttributedAssertion) and original.attributed_to
        target = target_of(original)
        witness_id = digest(snapshot.snapshot_id + target.model_dump_json())
        refs = original.evidence_references
        if not refs:
            limitations.append("ACCOUNT_WITHOUT_EVIDENCE")
            continue
        own_ids = tuple(evidence_id(r) for r in refs)[:20]
        for ref in refs[:20]:
            registry[evidence_id(ref)] = EvidenceItem(
                evidence_id=evidence_id(ref), reference=ref
            )
        links = tuple(link for link in artifacts.entity_links if link.account == target)
        linked_entities = {
            accepted[m].resolved_entity_id
            for link in links
            for m in link.mentions
            if m in accepted and accepted[m].resolved_entity_id
        }
        # Related chronology is participant context, never attributed testimony.
        events = tuple(
            r
            for r in accepted.values()
            if isinstance(r.original, InvestigationEvent)
            and any(
                link.account == target_of(r.original)
                and any(
                    m in accepted and accepted[m].resolved_entity_id in linked_entities
                    for m in link.mentions
                )
                for link in artifacts.entity_links
            )
        )
        related = []
        if artifacts.result:
            for finding in artifacts.result.findings:
                pair = pairs.get(finding.pair_id)
                if (
                    not pair
                    or finding.status != "POTENTIAL_INCONSISTENCY"
                    or finding.verification != "INDEPENDENT_STRUCTURED_SUPPORT"
                ):
                    continue
                accounts = (pair.left.revision, pair.right.revision)
                if target not in {target_of(r.original) for r in accounts}:
                    continue
                if not all(
                    isinstance(r.original, AttributedAssertion)
                    and r.original.attribution_status == "known"
                    and accepted.get(target_of(r.original)) == r
                    for r in accounts
                ):
                    continue
                # Distinct immutable source documents; no single-account confrontation.
                if accounts[0].original.document_id == accounts[1].original.document_id:
                    continue
                account_refs = [r.original.evidence_references for r in accounts]
                if not all(account_refs):
                    continue
                if not all(
                    any(a.reference == ref for a in finding.accounts)
                    for refs_ in account_refs
                    for ref in refs_
                ):
                    continue
                related.append(finding)
        uncertainties = [
            "SOURCE_ATTRIBUTION_IS_NOT_PERSONAL_KNOWLEDGE",
            "SPEAKER_IDENTITIES_NOT_MERGED_BY_NAME",
            "PARTICIPANT_LINKS_DO_NOT_IDENTIFY_SPEAKERS",
        ]
        if original.stated_time is None or original.stated_time.kind in (
            "unknown",
            "relative",
            "approximate",
            "interval",
        ):
            uncertainties.append("UNCERTAIN_OR_MISSING_TIME")
        witnesses.append(
            WitnessDossier(
                witness_id=witness_id,
                label=original.attributed_to,
                accounts=(revision,),
                related_participants=links,
                chronology=((revision,) if original.stated_time is not None else ())
                + events,
                findings=tuple(related),
                uncertainties=tuple(uncertainties),
            )
        )

        def add(
            category: Category,
            wording: str,
            rationale: str,
            groups: tuple[tuple[str, ...], ...],
            suffix: str = "",
            witness_id: str = witness_id,
        ) -> None:
            if len(questions) >= 200:
                if "QUESTION_BUDGET" not in limitations:
                    limitations.append("QUESTION_BUDGET")
                return
            ids = tuple(dict.fromkeys(i for g in groups for i in g))
            identity = digest(witness_id + category + suffix)
            questions.append(
                QuestionDraft(
                    question_id=identity,
                    witness_id=witness_id,
                    category=category,
                    wording=wording,
                    rationale=rationale,
                    evidence_ids=ids,
                    order=len(questions),
                )
            )
            policies.append(
                QuestionPolicy(question_id=identity, required_groups=groups)
            )

        add(
            "FOUNDATION",
            "What is the basis of your knowledge of the quoted source account?",
            "Establish the basis and limits of the attributed account "
            "without assuming its truth.",
            (own_ids,),
        )
        add(
            "CLARIFICATION",
            "What did you mean by the quoted account, and what limits "
            "or qualifications should be understood?",
            "Clarify the account without supplying missing facts.",
            (own_ids,),
        )
        if original.stated_time is not None:
            add(
                "TIMELINE",
                "What timing or sequence did you intend in this account, "
                "and how certain are you?",
                "Clarify stated timing; unknown or overlapping dates do "
                "not establish sequence.",
                (own_ids,),
            )
        for finding in related:
            pair = pairs[finding.pair_id]
            groups = tuple(
                tuple(evidence_id(ref) for ref in r.original.evidence_references[:10])
                for r in (pair.left.revision, pair.right.revision)
            )
            for r in (pair.left.revision, pair.right.revision):
                for ref in r.original.evidence_references[:10]:
                    registry[evidence_id(ref)] = EvidenceItem(
                        evidence_id=evidence_id(ref), reference=ref
                    )
            add(
                "POTENTIAL_INCONSISTENCY",
                "How do you understand the difference between these two "
                "separately attributed source accounts?",
                "Explore an independently supported potential "
                "inconsistency, not a proven contradiction or dishonesty.",
                groups,
                finding.finding_id,
            )
            add(
                "FOLLOW_UP",
                "What context or alternative explanation could reconcile "
                "these accounts, and what source could clarify it?",
                "Test alternative explanations without treating the "
                "investigative hypothesis as fact.",
                groups,
                finding.finding_id,
            )
    if not witnesses:
        limitations.append("NO_ATTRIBUTABLE_REVIEWED_EVIDENCE")
    assert snapshot.artifact_digest is not None
    return PlanBasis(
        plan_id=plan_id,
        matter_id=snapshot.matter_id,
        thread_id=snapshot.thread_id,
        snapshot_id=snapshot.snapshot_id,
        artifact_digest=snapshot.artifact_digest,
        source_dependency_digest=snapshot.source_dependency_digest,
        review_dependency_digest=snapshot.review_dependency_digest,
        dependencies=snapshot.dependencies,
        coverage=coverage,
        outcome="BLOCKED"
        if not questions
        else "LIMITED"
        if limitations
        else "FULLY_SOURCED",
        limitations=tuple(dict.fromkeys(limitations)),
        witnesses=tuple(witnesses),
        evidence=tuple(registry[k] for k in sorted(registry)),
        questions=tuple(questions),
        policies=tuple(policies),
    )

"""Synthetic citation-recovery inputs; no opinion text or labels from the API."""

from datetime import date

import pytest
from pydantic import HttpUrl

from lextrace.domain.case import Case, Opinion
from lextrace.evaluation.benchmark import (
    BenchmarkConfig,
    BenchmarkInputs,
    CitationEvidence,
    OpinionMapping,
    PositiveAudit,
    QuerySelection,
    Span,
    digest,
)


@pytest.fixture
def benchmark_sample() -> tuple[list[Case], BenchmarkInputs, BenchmarkConfig]:
    cases = [
        Case(
            source_id=str(index),
            source_url=HttpUrl(
                f"https://www.courtlistener.com/opinion/{index}/sample/"
            ),
            name=f"Candidate {index} v. Respondent",
            date_filed=date(1995 + index % 15, 1, 1),
            court_id="ca2" if index % 2 else "scotus",
            docket_number=str(index),
            reporter_citations=[f"{index} F.3d {index + 10}"],
            opinions=[
                Opinion(
                    source_id=str(10000 + index),
                    kind="010combined",
                    text="Synthetic candidate text.",
                    text_source_field="plain_text",
                )
            ],
        )
        for index in range(1, 321)
    ]
    mappings = [
        OpinionMapping(
            opinion_id=case.opinions[0].source_id,
            case_id=case.source_id,
            date_filed=case.date_filed,
            court=case.court_id,
            case_name=case.name,
            reporter_citations=case.reporter_citations or [],
            aliases=[f"Candidate {case.source_id}"],
            payload_sha256=digest("synthetic mapping " + case.source_id),
        )
        for case in cases[:2]
    ]
    selections: list[QuerySelection] = []
    evidence: list[CitationEvidence] = []
    facts = " ".join(
        [
            "The claimant challenges the procedure used to resolve "
            "a contested administrative decision."
        ]
        * 15
    )
    text = (
        facts
        + " Candidate 1 v. Respondent, 1 F.3d 11. Candidate 2 v. Respondent, 2 F.3d 12."
    )
    for index in range(1, 26):
        case_id, opinion_id = str(1000 + index), str(2000 + index)
        cases.append(
            Case(
                source_id=case_id,
                source_url=HttpUrl(
                    f"https://www.courtlistener.com/opinion/{case_id}/query/"
                ),
                name=f"Query {index} v. Agency",
                date_filed=date(2010, 1, index),
                court_id="ca2",
                docket_number=case_id,
                opinions=[
                    Opinion(
                        source_id=opinion_id,
                        kind="010combined",
                        text=text,
                        text_source_field="plain_text",
                    )
                ],
            )
        )
        selections.append(
            QuerySelection(
                query_id=f"q{index:02}",
                source_case_id=case_id,
                source_opinion_id=opinion_id,
                source_text_sha256=digest(text),
                excerpt=Span(start=0, end=len(facts)),
                leakage_reviewer="synthetic reviewer",
                leakage_review_approved=True,
                known_alias_review_complete=True,
                selection_note="Synthetic facts passage.",
                audit_reviewer="synthetic reviewer" if index <= 15 else None,
                positive_audits=[
                    PositiveAudit(
                        case_id=str(target),
                        context="substantive",
                        citation_context=Span(start=len(facts), end=len(text)),
                        note="Synthetic audit only.",
                    )
                    for target in (1, 2)
                ]
                if index <= 15
                else [],
            )
        )
        evidence.append(
            CitationEvidence(
                citing_opinion_id=opinion_id,
                cited_opinion_ids=["10001", "10002"],
                relation_source="opinions_cited",
                payload_sha256=digest("synthetic evidence " + opinion_id),
                complete=True,
            )
        )
    return (
        cases,
        BenchmarkInputs(
            selections=selections,
            citations=evidence,
            opinion_mappings=mappings,
            litigation_review_note="Synthetic cases have no related litigation.",
            source_provenance="Synthetic test data, not acquired from CourtListener.",
        ),
        BenchmarkConfig(),
    )

"""Synthetic federal hierarchy and evidence-backed precedent-trace tests."""

from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import HttpUrl, ValidationError

from lextrace.api.app import create_app
from lextrace.config import AppSettings
from lextrace.corpus import serialize_cases
from lextrace.domain.case import Case, Opinion
from lextrace.evaluation.benchmark import CitationEvidence, OpinionMapping, digest
from lextrace.graph.build import build_graph
from lextrace.graph.contracts import GraphError, GraphEvidenceBundle
from lextrace.graph.courts import classify_authority, court_for
from lextrace.graph.intelligence import PrecedentIntelligence, doctrine_for_claim
from lextrace.graph.intelligence_contracts import CitationContext, TreatmentAnnotation
from lextrace.graph.store import CitationGraph
from lextrace.matter.contracts import (
    ArgumentFinding,
    ClaimAuthorityLink,
    LegalClaim,
    MatterAnalysis,
    MatterError,
    MatterEvidence,
    SourceSpan,
)
from lextrace.matter.store import MatterStore
from lextrace.retrieval.contracts import Diagnostics, RetrievalResult
from lextrace.retrieval.documents import Corpus
from lextrace.retrieval.engine import LexTraceRetriever
from lextrace.retrieval.passages import segment


def _case(
    identifier: str,
    opinion_id: str,
    name: str,
    year: int,
    citation: str,
    text: str,
    court: str = "ca2",
) -> Case:
    return Case(
        source_id=identifier,
        source_url=HttpUrl(f"https://www.courtlistener.com/opinion/{identifier}/test/"),
        name=name,
        date_filed=date(year, 1, 1),
        court_id=court,
        docket_number=identifier,
        reporter_citations=[citation],
        opinions=[
            Opinion(
                source_id=opinion_id,
                kind="majority",
                text=text,
                text_source_field="plain_text",
            )
        ],
    )


def _engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LexTraceRetriever:
    monkeypatch.chdir(tmp_path)
    cases = [
        _case(
            "1",
            "11",
            "Alpha v. State",
            2000,
            "123 F.3d 456",
            "Protected activity was central to the decision.\n\n"
            "The claim requires proof.",
        ),
        _case(
            "2",
            "22",
            "Beta v. State",
            2010,
            "234 F.3d 567",
            "Protected activity remains at issue. We overrule Alpha v. State, "
            "123 F.3d 456, on this point.",
        ),
        _case(
            "3",
            "33",
            "Gamma v. State",
            2020,
            "345 F.3d 678",
            "Protected activity is disputed. We distinguish Beta v. State, "
            "234 F.3d 567, on different facts.",
            "ca9",
        ),
    ]
    evidence = [
        CitationEvidence(
            citing_opinion_id=citing,
            cited_opinion_ids=[cited],
            relation_source="opinions-cited",
            payload_sha256=digest(citing),
            complete=True,
        )
        for citing, cited in (("22", "11"), ("33", "22"))
    ]
    mappings = [
        OpinionMapping(
            opinion_id=case.opinions[0].source_id,
            case_id=case.source_id,
            date_filed=case.date_filed,
            court=case.court_id,
            case_name=case.name,
            payload_sha256=digest(case.source_id),
        )
        for case in cases
    ]
    Path("cases.jsonl").write_text(serialize_cases(cases))
    Path("edges.json").write_text(
        GraphEvidenceBundle(
            citations=evidence,
            opinion_mappings=mappings,
            source_provenance="synthetic",
        ).model_dump_json()
    )
    build_graph(Path("cases.jsonl"), Path("edges.json"), Path("artifacts/graphs/test"))
    return LexTraceRetriever(
        Corpus(cases), graph=CitationGraph(Path("artifacts/graphs/test"))
    )


def test_federal_authority_relationships() -> None:
    assert court_for("S.D.N.Y.") == court_for("nysd")
    assert classify_authority("1", "scotus", "nysd").category == "CONTROLLING"
    assert classify_authority("1", "ca2", "nysd").category == "CONTROLLING"
    assert classify_authority("1", "nysd", "nysd").category == "SAME_COURT"
    assert classify_authority("1", "nyed", "nysd").category == "PERSUASIVE"
    assert classify_authority("1", "ca9", "nysd").category == "PERSUASIVE"
    assert classify_authority("1", "unknown", "nysd").category == "UNKNOWN"
    assert classify_authority("1", "ca2", None).category == "UNKNOWN"


def test_trace_context_treatment_and_doctrine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine(tmp_path, monkeypatch)
    trace = PrecedentIntelligence(engine).trace(
        "2",
        proposition="protected activity",
        forum_court="nysd",
        backward_depth=1,
        forward_depth=1,
    )
    assert trace.nodes[0].node.case_id == "2"
    assert {node.node.case_id for node in trace.nodes} == {"1", "2", "3"}
    assert [
        (edge.edge.citing_case_id, edge.edge.cited_case_id) for edge in trace.edges
    ] == [("2", "1"), ("3", "2")]
    assert [edge.treatment.generated_label for edge in trace.edges] == [
        "OVERRULES",
        "DISTINGUISHES",
    ]
    assert trace.edges[0].treatment.review_requirement == "REVIEW_REQUIRED"
    assert trace.context_recovered == 2
    assert trace.edges[0].context is not None
    passage = trace.edges[0].context.passage
    source = engine.corpus.by_id["2"].opinions[0].text
    assert source[passage.start : passage.end] == passage.text
    by_id = {node.node.case_id: node for node in trace.nodes}
    assert by_id["1"].authority.category == "CONTROLLING"
    assert by_id["3"].authority.category == "PERSUASIVE"
    result = doctrine_for_claim("claim", "protected activity", trace)
    assert [event.category for event in result.events] == [
        "RULE_OVERRULED",
        "RULE_DISTINGUISHED",
    ]
    assert all(event.supporting_passage.passage.text for event in result.events)
    assert result.impact.category == "INSUFFICIENT_EVIDENCE"
    trace.edges[1].treatment.review_state = "CONFIRMED"
    assert (
        doctrine_for_claim("claim", "protected activity", trace).impact.category
        == "WEAKENS"
    )
    assert result.state.synthesis[0].annotation_ids
    assert "incomplete" in result.state.coverage_warning
    engine.close()


def test_cutoff_bounds_and_unavailable_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine(tmp_path, monkeypatch)
    service = PrecedentIntelligence(engine)
    cutoff = service.trace("2", as_of_date=date(2015, 1, 1), max_nodes=2)
    assert [node.node.case_id for node in cutoff.nodes] == ["1", "2"]
    assert len(cutoff.edges) == 1
    assert len(service.trace("2", max_nodes=1).nodes) == 1
    capped = service.trace("2", max_edges=1)
    assert len(capped.edges) == 1
    assert capped.edges_examined == 1
    with pytest.raises(GraphError, match="unavailable"):
        service.trace("3", as_of_date=date(2015, 1, 1))
    with pytest.raises(GraphError, match="bounds"):
        service.trace("2", max_nodes=51)
    # A graph edge does not itself imply treatment or citation context.
    engine.corpus.by_id["2"].opinions[0].text = "Protected activity remains at issue."
    missing = service.trace("2", backward_depth=1, forward_depth=0)
    assert missing.edges[0].context is None
    assert missing.edges[0].treatment.generated_label == "UNKNOWN"
    assert doctrine_for_claim("claim", "protected activity", missing).events == []
    engine.close()


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("We follow", "FOLLOWS"),
        ("We apply", "APPLIES"),
        ("We rely on", "RELIES_ON"),
        ("We limit", "LIMITS"),
        ("We criticize", "CRITICIZES"),
        ("We do not follow", "CITES"),
        ("The rule was overruled by", "CITES"),
        ("We cite", "CITES"),
    ],
)
def test_treatment_requires_targeted_active_language(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phrase: str, expected: str
) -> None:
    engine = _engine(tmp_path, monkeypatch)
    engine.corpus.by_id["2"].opinions[
        0
    ].text = f"Protected activity matters. {phrase} Alpha v. State, 123 F.3d 456."
    trace = PrecedentIntelligence(engine).trace("2", backward_depth=1, forward_depth=0)
    assert trace.edges[0].treatment.generated_label == expected
    engine.close()


def test_treatment_review_preserves_generated_label_and_invalidates_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine(tmp_path, monkeypatch)
    store = MatterStore(tmp_path / "matter.sqlite3", tmp_path / "private")
    matter = store.create_matter("Test", court="nysd")
    document = store.add_document(
        matter.matter_id, "brief.txt", b"Protected activity matters."
    )
    section = store.sections(matter.matter_id, document.document_id)[0]
    claim = LegalClaim(
        claim_id="a" * 32,
        matter_id=matter.matter_id,
        document_id=document.document_id,
        exact_source_text="Protected activity matters.",
        normalized_proposition="protected activity",
        span=SourceSpan(
            document_id=document.document_id,
            section_id=section.section_id,
            start=0,
            end=len("Protected activity matters."),
        ),
    )
    store.save_analysis(
        MatterAnalysis(
            matter_id=matter.matter_id,
            document_id=document.document_id,
            issues=[],
            claims=[claim],
            citations=[],
            findings=[],
        )
    )
    trace = PrecedentIntelligence(engine).trace("2", proposition="protected activity")
    result = doctrine_for_claim(claim.claim_id, "protected activity", trace)
    store.save_doctrine(matter.matter_id, claim.claim_id, "identity", result)
    annotation = trace.edges[0].treatment
    reviewed = store.review_treatment(
        matter.matter_id, claim.claim_id, annotation.annotation_id, "REJECTED"
    )
    assert reviewed.review_state == "REJECTED"
    assert reviewed.generated_label == annotation.generated_label
    assert store.cached_doctrine(matter.matter_id, claim.claim_id, "identity") is None
    claim.verification_state = "SUPPORTED"
    store.update_claim(claim)
    assert (
        store.treatment_review(
            matter.matter_id, claim.claim_id, annotation.annotation_id
        )
        is not None
    )
    claim.normalized_proposition = "A changed proposition"
    store.update_claim(claim)
    assert (
        store.treatment_review(
            matter.matter_id, claim.claim_id, annotation.annotation_id
        )
        is None
    )
    with pytest.raises(MatterError, match="not found"):
        store.review_treatment(matter.matter_id, claim.claim_id, "missing", "CONFIRMED")
    with pytest.raises(ValidationError):
        TreatmentAnnotation.model_validate(annotation.model_dump() | {"passage": None})
    assert annotation.passage is not None
    with pytest.raises(ValidationError):
        CitationContext.model_validate(
            annotation.passage.model_dump(mode="json")
            | {"source_url": "https://example.com/opinion/2/"}
        )
    store.close()
    engine.close()


def test_precedent_api_doctrine_and_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine(tmp_path, monkeypatch)
    database = tmp_path / "api.sqlite3"
    private = tmp_path / "private-api"
    store = MatterStore(database, private)
    matter = store.create_matter("Forum test", court="nysd")
    document = store.add_document(
        matter.matter_id, "brief.txt", b"Protected activity matters."
    )
    section = store.sections(matter.matter_id, document.document_id)[0]
    claim = LegalClaim(
        claim_id="b" * 32,
        matter_id=matter.matter_id,
        document_id=document.document_id,
        exact_source_text="Protected activity matters.",
        normalized_proposition="protected activity",
        span=section.span,
    )
    authority_case = engine.corpus.by_id["2"]
    evidence = MatterEvidence(
        evidence_id="c" * 32,
        claim_id=claim.claim_id,
        role="cited",
        retrieval_query="protected activity",
        result=RetrievalResult(
            case_id="2",
            rank=1,
            final_score=1.0,
            retrieval_method="bm25",
            case_name=authority_case.name,
            court=authority_case.court_id,
            date_filed=authority_case.date_filed,
            reporter_citations=authority_case.reporter_citations,
            source_url=authority_case.source_url,
            relevant_passage=segment(authority_case, engine.config.passages)[0],
            diagnostics=Diagnostics(),
        ),
    )
    finding = ArgumentFinding(
        claim_id=claim.claim_id,
        issue_id=None,
        proposition=claim.normalized_proposition,
        source=claim.span,
        cited_authorities=[
            ClaimAuthorityLink(
                claim_id=claim.claim_id,
                case_id="2",
                relation="CITES",
                citation_id="citation",
                evidence_id=evidence.evidence_id,
            )
        ],
        evidence=[evidence],
        counter_authorities=[],
        unresolved_citation_ids=[],
        verification_status="INSUFFICIENT_EVIDENCE",
        research_coverage="SEARCHED",
        vulnerability="INSUFFICIENT_EVIDENCE",
        explanation="Synthetic fixture.",
    )
    store.save_analysis(
        MatterAnalysis(
            matter_id=matter.matter_id,
            document_id=document.document_id,
            issues=[],
            claims=[claim],
            citations=[],
            findings=[finding],
        )
    )
    store.close()
    base = f"/matters/{matter.matter_id}/claims/{claim.claim_id}"
    with TestClient(
        create_app(
            retriever=engine,
            settings=AppSettings(matter_db=database, private_matter_root=private),
        )
    ) as client:
        assert client.get("/cases/2/precedent-trace").status_code == 200
        trace = client.get(f"{base}/precedent-trace")
        assert trace.status_code == 200
        assert trace.json()["context_recovered"] == 2
        authority = client.get(f"{base}/authority-analysis")
        assert authority.status_code == 200
        assert authority.json()["relationships"][0]["category"] == "CONTROLLING"
        doctrine = client.get(f"{base}/doctrine")
        assert doctrine.status_code == 200
        assert doctrine.json()["cache_hit"] is False
        assert client.get(f"{base}/doctrine").json()["cache_hit"] is True
        assert len(doctrine.json()["events"]) == 2
        annotation = doctrine.json()["trace"]["edges"][0]["treatment"]
        reviewed = client.patch(
            f"{base}/treatments/{annotation['annotation_id']}",
            json={"state": "REJECTED"},
        )
        assert reviewed.status_code == 200
        assert reviewed.json()["generated_label"] == annotation["generated_label"]
        refreshed = client.get(f"{base}/doctrine").json()
        assert refreshed["cache_hit"] is False
        assert len(refreshed["events"]) == 1
        assert refreshed["impact"]["category"] == "INSUFFICIENT_EVIDENCE"
        later = doctrine.json()["trace"]["edges"][1]["treatment"]
        assert (
            client.patch(
                f"{base}/treatments/{later['annotation_id']}",
                json={"state": "CONFIRMED"},
            ).status_code
            == 200
        )
        assert client.get(f"{base}/doctrine").json()["impact"]["category"] == "WEAKENS"
        assert client.get(f"{base}/precedent-trace?seed_case_id=999").status_code == 400

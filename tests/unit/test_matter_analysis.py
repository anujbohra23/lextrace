"""Offline X-Ray provenance, citation resolution, and coverage categories."""

from datetime import date
from pathlib import Path
from typing import TypeVar, cast

import pytest
from pydantic import BaseModel, HttpUrl, ValidationError

from lextrace.domain.case import Case, Opinion
from lextrace.matter.analysis import MatterAnalyzer, classify_finding, extract_citations
from lextrace.matter.contracts import MatterEvidence
from lextrace.matter.documents import extract_document, segment_document
from lextrace.matter.store import MatterStore
from lextrace.research.contracts import Usage, VerificationStatus
from lextrace.research.prompts import PromptDefinition
from lextrace.retrieval.contracts import SearchRequest, SearchResponse
from lextrace.retrieval.documents import Corpus
from lextrace.retrieval.engine import LexTraceRetriever

Output = TypeVar("Output", bound=BaseModel)


class FakeMatterLLM:
    provider = "offline-test"
    model = "deterministic"
    retries = 0

    def __init__(self) -> None:
        self.usage = Usage()
        self.prompts: list[PromptDefinition] = []

    def generate(
        self, prompt: PromptDefinition, schema: type[Output], context: dict[str, object]
    ) -> Output:
        self.prompts.append(prompt)
        self.usage.calls += 1
        sections = context.get("sections")
        section_id = ""
        if isinstance(sections, list):
            section = next(
                (
                    item
                    for item in sections
                    if isinstance(item, dict) and "A plaintiff" in str(item.get("text"))
                ),
                None,
            )
            if isinstance(section, dict):
                section_id = str(section["section_id"])
        if prompt.prompt_id == "matter-issues":
            value: object = {
                "issues": [{"label": "Retaliation", "section_ids": [section_id]}]
            }
        elif prompt.prompt_id == "matter-claims":
            value = {
                "claims": [
                    {
                        "exact_quote": (
                            "A plaintiff must show protected activity and a "
                            "materially adverse action."
                        ),
                        "section_id": section_id,
                        "normalized_proposition": (
                            "Protected activity and adverse action are required."
                        ),
                        "issue_label": "Retaliation",
                        "importance": "high",
                    },
                    {
                        "exact_quote": (
                            "A fabricated legal rule never appearing in the source."
                        ),
                        "section_id": section_id,
                        "normalized_proposition": "Fabricated.",
                    },
                ]
            }
        elif prompt.prompt_id == "matter-counter":
            value = {"query": "limitations on retaliation claims"}
        else:
            evidence = context.get("evidence")
            assert isinstance(evidence, list)
            value = {
                "judgments": [
                    {
                        "evidence_id": item["evidence_id"],
                        "status": "UNSUPPORTED"
                        if item["role"] == "counter"
                        else "SUPPORTED",
                        "supporting_passage_ids": []
                        if item["role"] == "counter"
                        else [item["passage_id"]],
                        "explanation": "Deterministic test judgment.",
                    }
                    for item in evidence
                    if isinstance(item, dict)
                ]
            }
        return schema.model_validate(value)


def _engine(monkeypatch: pytest.MonkeyPatch) -> LexTraceRetriever:
    case = Case(
        source_id="1",
        source_url=HttpUrl("https://www.courtlistener.com/opinion/1/example/"),
        name="Smith v. Example",
        date_filed=date(2009, 1, 1),
        court_id="ca2",
        docket_number="1",
        reporter_citations=["123 F.3d 456"],
        opinions=[
            Opinion(
                source_id="11",
                kind="majority",
                text=(
                    "Protected activity and a materially adverse action "
                    "are elements of this claim."
                ),
                text_source_field="plain_text",
            )
        ],
    )
    engine = LexTraceRetriever(Corpus([case]), mode="bm25")
    original = engine.search_response

    def lexical(request: SearchRequest) -> SearchResponse:
        return original(request.model_copy(update={"mode": "bm25"}))

    monkeypatch.setattr(engine, "search_response", lexical)
    return engine


@pytest.mark.parametrize("duplicate_claims", [False, True])
def test_analysis_preserves_spans_and_uses_canonical_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, duplicate_claims: bool
) -> None:
    text = (Path(__file__).parents[1] / "fixtures" / "matter_brief.txt").read_text()
    store = MatterStore(tmp_path / "matter.sqlite3", tmp_path / "private")
    matter = store.create_matter("Test")
    document = store.add_document(matter.matter_id, "brief.txt", text.encode())
    engine = _engine(monkeypatch)
    llm = FakeMatterLLM()
    if duplicate_claims:
        original_generate = llm.generate

        def repeat_claims(
            prompt: PromptDefinition,
            schema: type[Output],
            context: dict[str, object],
        ) -> Output:
            output = original_generate(prompt, schema, context)
            if prompt.prompt_id == "matter-claims":
                data = output.model_dump()
                data["claims"] = [data["claims"][0]] * 3
                return schema.model_validate(data)
            return output

        monkeypatch.setattr(llm, "generate", repeat_claims)
    result = MatterAnalyzer(engine, llm).analyze(
        matter.matter_id,
        document.document_id,
        text,
        store.sections(matter.matter_id, document.document_id),
    )
    assert len(result.claims) == 1
    assert llm.usage.calls == 4
    claim = result.claims[0]
    assert text[claim.span.start : claim.span.end] == claim.exact_source_text
    assert "fabricated" not in claim.normalized_proposition.lower()
    assert result.citations[0].exact_text == "123 F.3d 456"
    assert result.citations[0].parsed_case_name == "Smith v. Example"
    assert result.citations[0].resolved_case_id == "1"
    assert result.citations[1].resolution_status == "NOT_FOUND"
    finding = result.findings[0]
    assert finding.vulnerability == "MIXED"
    assert finding.cited_authorities[0].case_id == "1"
    assert all(e.result.case_id in engine.corpus.by_id for e in finding.evidence)
    assert all(
        e.result.relevant_passage.text
        in engine.corpus.by_id[e.result.case_id].opinions[0].text
        for e in finding.evidence
    )
    assert all("untrusted" in prompt.instructions for prompt in llm.prompts)
    store.save_analysis(result)
    assert store.analysis(matter.matter_id, document.document_id) == result
    store.close()
    engine.close()


@pytest.mark.parametrize(
    ("cited", "support", "counter", "searched", "unresolved", "expected"),
    [
        (["SUPPORTED"], ["SUPPORTED"], [], True, False, "STRONG"),
        (["PARTIALLY_SUPPORTED"], [], [], True, False, "MIXED"),
        (["UNSUPPORTED"], [], [], True, False, "UNSUPPORTED"),
        (["INSUFFICIENT_EVIDENCE"], [], [], True, False, "INSUFFICIENT_EVIDENCE"),
        (["SUPPORTED"], [], ["CONTRADICTED"], True, False, "VULNERABLE"),
        ([], [], [], True, True, "INSUFFICIENT_EVIDENCE"),
        ([], [], [], False, False, "INSUFFICIENT_EVIDENCE"),
    ],
)
def test_coverage_categories(
    cited: list[str],
    support: list[str],
    counter: list[str],
    searched: bool,
    unresolved: bool,
    expected: str,
) -> None:
    category, explanation = classify_finding(
        cast(list[VerificationStatus], cited),
        cast(list[VerificationStatus], support),
        cast(list[VerificationStatus], counter),
        searched=searched,
        unresolved=unresolved,
        independent_distinct=True,
    )
    assert category == expected
    assert explanation


def test_ambiguous_reporter_never_selects_a_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(monkeypatch)
    first = engine.corpus.cases[0]
    second = first.model_copy(update={"source_id": "2", "name": "Another case"})
    duplicated = LexTraceRetriever(Corpus([first, second]), mode="bm25")
    text = "Smith v. Example, 123 F.3d 456. Missing v. Case, 999 F.3d 999."
    sections = segment_document(
        "doc", "hash", extract_document("brief.txt", text.encode())
    )
    citations = extract_citations("matter", "doc", text, sections)
    MatterAnalyzer(duplicated, FakeMatterLLM())._resolve(citations)
    assert [c.resolution_status for c in citations] == ["AMBIGUOUS", "NOT_FOUND"]
    assert all(c.resolved_case_id is None for c in citations)
    duplicated.close()
    engine.close()


class InventedEvidenceLLM(FakeMatterLLM):
    def generate(
        self, prompt: PromptDefinition, schema: type[Output], context: dict[str, object]
    ) -> Output:
        if prompt.prompt_id == "matter-verification":
            return schema.model_validate(
                {
                    "judgments": [
                        {
                            "evidence_id": "invented-evidence",
                            "status": "SUPPORTED",
                            "supporting_passage_ids": ["invented-passage"],
                            "explanation": "This must not be accepted.",
                        }
                    ]
                }
            )
        return super().generate(prompt, schema, context)


def test_invented_passage_cannot_support_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MatterStore(tmp_path / "db", tmp_path / "private")
    matter = store.create_matter("Test")
    text = (
        "A plaintiff must show protected activity and a materially adverse "
        "action. Smith v. Example, 123 F.3d 456."
    )
    document = store.add_document(matter.matter_id, "brief.txt", text.encode())
    engine = _engine(monkeypatch)
    result = MatterAnalyzer(engine, InventedEvidenceLLM()).analyze(
        matter.matter_id,
        document.document_id,
        text,
        store.sections(matter.matter_id, document.document_id),
    )
    assert result.findings[0].verification_status == "INSUFFICIENT_EVIDENCE"
    assert result.findings[0].vulnerability == "INSUFFICIENT_EVIDENCE"
    engine.close()
    store.close()


def test_partial_document_coverage_is_exposed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = (
        "A plaintiff must show protected activity and a materially adverse "
        "action. Smith v. Example, 123 F.3d 456."
        + "\n\n"
        + "\n\n".join(f"Additional legal paragraph {i}." for i in range(30))
    )
    store = MatterStore(tmp_path / "db", tmp_path / "private")
    matter = store.create_matter("Test")
    document = store.add_document(matter.matter_id, "brief.txt", text.encode())
    engine = _engine(monkeypatch)
    result = MatterAnalyzer(engine, FakeMatterLLM()).analyze(
        matter.matter_id,
        document.document_id,
        text,
        store.sections(matter.matter_id, document.document_id),
    )
    assert result.warnings
    assert result.warnings == result.findings[0].warnings
    store.save_analysis(result)
    saved = store.get_document(matter.matter_id, document.document_id)
    assert saved is not None and saved.analysis_warnings
    engine.close()
    store.close()


def test_external_evidence_link_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _engine(monkeypatch)
    result = engine.search_response(
        SearchRequest(query="protected activity", mode="bm25", top_k=1)
    ).results[0]
    forged = result.model_copy(
        update={"source_url": HttpUrl("https://example.invalid/opinion/1/")}
    )
    with pytest.raises(ValidationError, match="CourtListener URL"):
        MatterEvidence(
            evidence_id="e",
            claim_id="c",
            role="independent_support",
            retrieval_query="protected activity",
            result=forged,
        )
    engine.close()

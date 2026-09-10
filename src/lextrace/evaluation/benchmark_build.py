"""Offline construction and verification of frozen citation-recovery bundles."""

import json
import re
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Literal, TypeVar

from pydantic import BaseModel, ValidationError

from lextrace.corpus import CorpusError, read_cases, serialize_cases
from lextrace.domain.case import Case
from lextrace.evaluation.benchmark import (
    BenchmarkConfig,
    BenchmarkError,
    BenchmarkInputs,
    BenchmarkItem,
    BenchmarkManifest,
    Exclusion,
    ExclusionReason,
    OpinionMapping,
    QuerySelection,
    canonical,
    digest,
)

T = TypeVar("T", bound=BaseModel)


def load_model(path: Path, model: type[T]) -> T:
    try:
        return model.model_validate_json(path.read_bytes())
    except (OSError, ValidationError):
        raise BenchmarkError(
            "Could not read a valid benchmark input or metadata file."
        ) from None


def _unique(values: list[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise BenchmarkError(f"Duplicate {label}.")


def _eligible(case: Case, config: BenchmarkConfig) -> bool:
    return (
        case.date_filed is not None
        and case.date_filed <= config.candidate_cutoff
        and case.court_id in config.candidate_courts
    )


def _stratum(case: Case) -> str:
    assert case.date_filed is not None
    return f"{case.court_id}:{case.date_filed.year}"


def _scrub(selection: QuerySelection, text: str) -> str:
    if digest(text) != selection.source_text_sha256:
        raise BenchmarkError("Query source-text hash mismatch.")
    start, end = selection.excerpt.start, selection.excerpt.end
    if end > len(text):
        raise BenchmarkError("Query excerpt exceeds source text.")
    cursor = start
    parts: list[str] = []
    for removal in sorted(selection.removals, key=lambda item: (item.start, item.end)):
        if removal.start < cursor or removal.end > end:
            raise BenchmarkError("Query removals overlap or fall outside the excerpt.")
        parts.append(text[cursor : removal.start])
        cursor = removal.end
    parts.append(text[cursor:end])
    return " ".join(" ".join(parts).split())


def _check_leakage(query: str, terms: list[str]) -> None:
    normalized = " ".join(query.casefold().split())
    for term in terms:
        term = " ".join(term.casefold().split())
        if term and re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", normalized):
            raise BenchmarkError(
                "Query contains a known case reference or reporter citation."
            )
    patterns = [
        r"\b(?:id\.|ibid\.|supra\b|infra\b)",
        r"\bv(?:s)?\.\s+\S",
        r"\b\d+\s+(?:U\.?\s*S\.?|S\.?\s*Ct\.?|F\.?\s*(?:2d|3d|4th|Supp\.?|App'?x))\s*\d",
    ]
    if any(re.search(pattern, query, flags=re.IGNORECASE) for pattern in patterns):
        raise BenchmarkError(
            "Query contains a detectable citation or short-form reference."
        )


def _canonical_inputs(inputs: BenchmarkInputs) -> BenchmarkInputs:
    data = inputs.model_dump(mode="json")
    data["selections"] = sorted(data["selections"], key=lambda item: item["query_id"])
    for selection in data["selections"]:
        selection["removals"].sort(key=lambda item: (item["start"], item["end"]))
        selection["positive_audits"].sort(key=lambda item: int(item["case_id"]))
    data["citations"].sort(key=lambda item: int(item["citing_opinion_id"]))
    for evidence in data["citations"]:
        evidence["cited_opinion_ids"] = sorted(
            set(evidence["cited_opinion_ids"]), key=int
        )
    data["opinion_mappings"].sort(key=lambda item: int(item["opinion_id"]))
    for mapping in data["opinion_mappings"]:
        mapping["aliases"] = sorted(set(mapping["aliases"]))
        mapping["reporter_citations"] = sorted(set(mapping["reporter_citations"]))
    for group in data["litigation_groups"]:
        data["litigation_groups"][group] = sorted(
            data["litigation_groups"][group], key=int
        )
    return BenchmarkInputs.model_validate(data)


def _code_digest() -> str:
    # Content fingerprints avoid clock, Git dirty-state, and path-dependent output.
    root = Path(__file__).parents[1]
    files = [
        "evaluation/benchmark.py",
        "evaluation/benchmark_build.py",
        "evaluation/excerpts.py",
        "ingestion/benchmark_sources.py",
        "ingestion/benchmark_acquisition.py",
        "ingestion/benchmark_job.py",
        "domain/case.py",
        "corpus.py",
        "ingestion/normalize.py",
    ]
    return digest(
        "\n".join(name + ":" + digest((root / name).read_bytes()) for name in files)
    )


def assemble(
    cases: list[Case],
    inputs: BenchmarkInputs,
    config: BenchmarkConfig,
) -> dict[str, str]:
    """Build only from local frozen data; no network, clock, or retrieval method."""
    _unique([case.source_id for case in cases], "source case IDs")
    _unique(
        [op.source_id for case in cases for op in case.opinions], "source opinion IDs"
    )
    if any(not re.fullmatch(r"[1-9][0-9]*", case.source_id) for case in cases):
        raise BenchmarkError("Invalid source case ID.")
    if any(
        not re.fullmatch(r"[1-9][0-9]*", op.source_id)
        for case in cases
        for op in case.opinions
    ):
        raise BenchmarkError("Invalid source opinion ID.")
    _unique([item.query_id for item in inputs.selections], "query IDs")
    _unique([item.source_case_id for item in inputs.selections], "query source cases")
    _unique([item.citing_opinion_id for item in inputs.citations], "citation evidence")
    _unique([item.opinion_id for item in inputs.opinion_mappings], "opinion mappings")
    if len(inputs.selections) != config.query_count:
        raise BenchmarkError("V1 requires exactly 25 reviewed query selections.")
    inputs = _canonical_inputs(inputs)
    by_id = {case.source_id: case for case in cases}
    evidence = {item.citing_opinion_id: item for item in inputs.citations}
    mappings = {item.opinion_id: item for item in inputs.opinion_mappings}
    if set(evidence) != {
        selection.source_opinion_id for selection in inputs.selections
    }:
        raise BenchmarkError(
            "Citation evidence must reference exactly the selected source opinions."
        )
    if set(mappings) != {
        identifier
        for relation in inputs.citations
        for identifier in relation.cited_opinion_ids
    }:
        raise BenchmarkError("Missing or unused opinion-to-cluster mapping evidence.")
    for frozen_mapping in mappings.values():
        if frozen_mapping.case_id is not None and frozen_mapping.case_id in by_id:
            mapped_case = by_id[frozen_mapping.case_id]
            if (
                mapped_case.date_filed != frozen_mapping.date_filed
                or mapped_case.court_id != frozen_mapping.court
                or mapped_case.name != frozen_mapping.case_name
                or not any(
                    op.source_id == frozen_mapping.opinion_id
                    for op in mapped_case.opinions
                )
            ):
                raise BenchmarkError(
                    "Citation mapping conflicts with its source Case/opinion."
                )
    groups: dict[str, str] = {}
    for group, members in inputs.litigation_groups.items():
        for member in members:
            if member in groups:
                raise BenchmarkError("A litigation member occurs in multiple groups.")
            groups[member] = group
    known_ids = set(by_id) | {
        item.case_id for item in mappings.values() if item.case_id
    }
    if not set(groups) <= known_ids:
        raise BenchmarkError("Litigation group refers to an unknown case.")
    selected: list[tuple[Case, QuerySelection]] = []
    for selection in inputs.selections:
        case = by_id.get(selection.source_case_id)
        if case is None or case.date_filed is None:
            raise BenchmarkError("Query source case or filing date is missing.")
        if (
            case.court_id != config.query_court
            or not config.query_date_start <= case.date_filed <= config.query_date_end
        ):
            raise BenchmarkError("Query falls outside the V1 jurisdiction/date range.")
        selected.append((case, selection))
    selected.sort(key=lambda pair: (pair[0].date_filed, int(pair[0].source_id)))
    group_splits: dict[str, str] = {}
    items: list[BenchmarkItem] = []
    exclusions: list[Exclusion] = []
    audited: list[str] = []
    positive_union: set[str] = set()
    for index, (case, selection) in enumerate(selected):
        split: Literal["dev", "test"] = "dev" if index < config.dev_count else "test"
        query_group = groups.get(case.source_id)
        if (
            query_group
            and query_group in group_splits
            and group_splits[query_group] != split
        ):
            raise BenchmarkError(
                "Related litigation crosses the temporal dev/test boundary; "
                "revise selections."
            )
        if query_group:
            group_splits[query_group] = split
        opinion = next(
            (op for op in case.opinions if op.source_id == selection.source_opinion_id),
            None,
        )
        if opinion is None or opinion.kind not in {"010combined", "020lead"}:
            raise BenchmarkError(
                "Query must reference a lead or combined opinion in its source Case."
            )
        relations = evidence.get(opinion.source_id)
        if relations is None:
            raise BenchmarkError(
                "Missing complete citation evidence for a query opinion."
            )
        positives: set[str] = set()
        terms = [case.name, *(case.reporter_citations or [])]
        for opinion_id in relations.cited_opinion_ids:
            mapping = mappings.get(opinion_id)
            if mapping is None:
                raise BenchmarkError("Missing opinion-to-cluster mapping evidence.")
            terms.extend(
                [mapping.case_name or "", *mapping.aliases, *mapping.reporter_citations]
            )
            reason = _exclusion(mapping, case, groups, config)
            if reason:
                exclusions.append(
                    Exclusion(
                        query_id=selection.query_id,
                        cited_opinion_id=opinion_id,
                        reason=reason,
                    )
                )
                continue
            target = by_id.get(mapping.case_id or "")
            if target is None:
                raise BenchmarkError(
                    "An eligible positive Case is missing "
                    "from the frozen source corpus."
                )
            if (
                target.date_filed != mapping.date_filed
                or target.court_id != mapping.court
                or target.name != mapping.case_name
                or not any(op.source_id == opinion_id for op in target.opinions)
            ):
                raise BenchmarkError(
                    "Citation mapping conflicts with its source Case/opinion."
                )
            assert case.date_filed is not None and target.date_filed is not None
            if target.date_filed >= case.date_filed:
                raise BenchmarkError("Temporal leakage detected.")
            terms.extend([target.name, *(target.reporter_citations or [])])
            positives.add(target.source_id)
        if len(positives) < config.minimum_positives:
            raise BenchmarkError(
                "Query has fewer than two eligible citation-derived positives."
            )
        query_text = _scrub(selection, opinion.text)
        if (
            not config.excerpt_min_words
            <= len(query_text.split())
            <= config.excerpt_max_words
        ):
            raise BenchmarkError(
                "Scrubbed query must contain 150–300 whitespace-delimited words."
            )
        _check_leakage(query_text, terms)
        if selection.audit_reviewer is not None:
            _unique(
                [audit.case_id for audit in selection.positive_audits],
                "positive audit IDs",
            )
            if {audit.case_id for audit in selection.positive_audits} != positives:
                raise BenchmarkError(
                    "Detailed audit must cover every eligible positive exactly once."
                )
            if any(
                audit.citation_context.end > len(opinion.text)
                for audit in selection.positive_audits
            ):
                raise BenchmarkError("Audit citation context exceeds source text.")
            audited.append(selection.query_id)
        elif selection.positive_audits:
            raise BenchmarkError("Positive audit records require a reviewer.")
        positive_union.update(positives)
        assert case.date_filed is not None
        items.append(
            BenchmarkItem(
                query_id=selection.query_id,
                source_case_id=case.source_id,
                source_opinion_id=opinion.source_id,
                query_text=query_text,
                query_date=case.date_filed,
                query_court="ca2",
                positive_case_ids=sorted(positives, key=int),
                split=split,
                query_provenance_id=selection.query_id,
            )
        )
    reviewed = all(s.review_status == "reviewed" for s in inputs.selections)
    if reviewed and len(audited) != config.detailed_audit_queries:
        raise BenchmarkError("V1 requires 15 fully audited queries.")
    pool = {case.source_id: case for case in cases if _eligible(case, config)}
    if len(positive_union) > config.candidate_max:
        raise BenchmarkError("Positive union exceeds 500; do not truncate labels.")
    target_size = max(config.candidate_target, len(positive_union))
    if len(pool) < config.candidate_min:
        raise BenchmarkError("Fewer than 200 eligible candidate Cases are available.")
    candidate_ids = set(positive_union)
    strata: dict[str, list[str]] = defaultdict(list)
    for identifier, candidate in pool.items():
        if identifier not in candidate_ids:
            strata[_stratum(candidate)].append(identifier)
    for key, identifiers in strata.items():
        identifiers.sort(
            key=lambda identifier: (
                digest(f"{config.selection_seed}:{key}:{identifier}"),
                int(identifier),
            )
        )
    # Equal allocation rounds across court/year strata; exhausted strata drop out.
    positions = {key: 0 for key in strata}
    while len(candidate_ids) < min(target_size, len(pool)):
        for key in sorted(strata):
            if len(candidate_ids) >= min(target_size, len(pool)):
                break
            if positions[key] < len(strata[key]):
                candidate_ids.add(strata[key][positions[key]])
                positions[key] += 1
    candidates = [pool[identifier] for identifier in sorted(candidate_ids, key=int)]
    for item in items:
        if not set(item.positive_case_ids) <= candidate_ids:
            raise BenchmarkError("Missing candidate positive.")
        if any(
            candidate.date_filed is None or candidate.date_filed >= item.query_date
            for candidate in candidates
        ):
            raise BenchmarkError("Temporal leakage in candidate set.")
    artifact = {
        "sources.jsonl": serialize_cases(cases),
        "provenance.json": canonical(inputs) + "\n",
        "config.json": canonical(config) + "\n",
        "queries.jsonl": "".join(canonical(item) + "\n" for item in items),
        "candidates.jsonl": serialize_cases(candidates),
    }
    final_strata: dict[str, list[str]] = defaultdict(list)
    for candidate in candidates:
        final_strata[_stratum(candidate)].append(candidate.source_id)
    manifest = BenchmarkManifest(
        review_status="reviewed" if reviewed else "review_required",
        config=config,
        code_sha256=_code_digest(),
        source_corpus_sha256=digest(artifact["sources.jsonl"]),
        provenance_sha256=digest(artifact["provenance.json"]),
        queries_sha256=digest(artifact["queries.jsonl"]),
        candidates_sha256=digest(artifact["candidates.jsonl"]),
        query_case_ids=[item.source_case_id for item in items],
        candidate_case_ids=sorted(candidate_ids, key=int),
        positive_union_ids=sorted(positive_union, key=int),
        candidate_strata=dict(final_strata),
        split_query_ids={
            split: [item.query_id for item in items if item.split == split]
            for split in ("dev", "test")
        },
        audited_query_ids=audited,
        leakage_reviewed_queries=sum(
            s.review_status == "reviewed" for s in inputs.selections
        ),
        exclusions=exclusions,
        exclusion_counts=dict(Counter(item.reason for item in exclusions)),
    )
    artifact["manifest.json"] = canonical(manifest) + "\n"
    artifact["audit.json"] = (
        json.dumps(
            [
                {
                    "query_id": item.query_id,
                    "source_case_id": item.source_case_id,
                    "case_name": by_id[item.source_case_id].name,
                    "date": item.query_date.isoformat(),
                    "excerpt": item.query_text,
                    "positive_case_ids": item.positive_case_ids,
                    "depths_by_opinion_id": evidence[item.source_opinion_id].depths,
                    "exclusions": [
                        e.model_dump()
                        for e in exclusions
                        if e.query_id == item.query_id
                    ],
                    "automated_leakage_check": "passed",
                    "human_review_status": next(
                        s.review_status
                        for s in inputs.selections
                        if s.query_id == item.query_id
                    ),
                }
                for item in items
            ],
            sort_keys=True,
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    return artifact


def _exclusion(
    mapping: OpinionMapping,
    query: Case,
    groups: dict[str, str],
    config: BenchmarkConfig,
) -> ExclusionReason | None:
    if mapping.case_id is None:
        return "unresolved"
    if mapping.case_id == query.source_id:
        return "self"
    if (
        groups.get(query.source_id) is not None
        and groups.get(mapping.case_id) == groups[query.source_id]
    ):
        return "same_litigation"
    if mapping.date_filed is None:
        return "missing_date"
    if mapping.court not in config.candidate_courts:
        return "out_of_scope_court"
    if mapping.date_filed > config.candidate_cutoff:
        return "after_cutoff"
    return None


def build_benchmark(
    corpus: Path, inputs_path: Path, config_path: Path, output: Path
) -> dict[str, object]:
    if not output.resolve().is_relative_to(Path("data/benchmarks/v1").resolve()):
        raise BenchmarkError("Benchmark bundles must be under data/benchmarks/v1/.")
    if output.exists():
        raise BenchmarkError(
            "Benchmark output already exists; use a new bundle directory."
        )
    try:
        cases = read_cases(corpus)
    except CorpusError:
        raise BenchmarkError("Invalid benchmark source corpus.") from None
    artifacts = assemble(
        cases,
        load_model(inputs_path, BenchmarkInputs),
        load_model(config_path, BenchmarkConfig),
    )
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            dir=output.parent, prefix=".benchmark-"
        ) as temporary:
            bundle = Path(temporary) / "bundle"
            bundle.mkdir()
            for name, content in artifacts.items():
                (bundle / name).write_text(content, encoding="utf-8", newline="\n")
            bundle.rename(output)
    except OSError:
        raise BenchmarkError("Could not write benchmark bundle.") from None
    return {
        "status": "built",
        "queries": 25,
        "candidate_cases": len(artifacts["candidates.jsonl"].splitlines()),
    }


def validate_benchmark(bundle: Path) -> dict[str, object]:
    """Reconstruct from frozen evidence and compare every output byte."""
    config = load_model(bundle / "config.json", BenchmarkConfig)
    inputs = load_model(bundle / "provenance.json", BenchmarkInputs)
    try:
        cases = read_cases(bundle / "sources.jsonl")
        expected = assemble(cases, inputs, config)
        if any(
            (bundle / name).read_bytes() != content.encode("utf-8")
            for name, content in expected.items()
        ):
            raise BenchmarkError(
                "Benchmark artifacts, hashes, or generation code do not match."
            )
    except (OSError, CorpusError):
        raise BenchmarkError("Missing or invalid benchmark artifacts.") from None
    return {
        "status": "valid",
        "queries": config.query_count,
        "candidate_cases": len(expected["candidates.jsonl"].splitlines()),
    }

"""Resumable acquisition of benchmark inputs; no retrieval-informed selection."""

import json
from datetime import date
from pathlib import Path

import httpx
from pydantic import BaseModel

from lextrace.corpus import atomic_write, read_cases, serialize_cases
from lextrace.domain.case import Case
from lextrace.evaluation.benchmark import (
    BenchmarkConfig,
    BenchmarkError,
    BenchmarkInputs,
    CitationEvidence,
    OpinionMapping,
    QuerySelection,
    canonical,
    digest,
)
from lextrace.evaluation.benchmark_build import load_model
from lextrace.evaluation.excerpts import propose_excerpt
from lextrace.ingestion.benchmark_acquisition import OPINION_FIELDS, Acquisition, parse
from lextrace.ingestion.benchmark_sources import resource_id
from lextrace.ingestion.courtlistener import (
    DocketResponse,
    IngestionError,
    RequestFailure,
    linked_id,
)


class JobProgress(BaseModel):
    rejections: list[dict[str, str]]


def acquire_benchmark(
    output: Path,
    token: str,
    *,
    interval: float = 15,
    timeout: float = 90,
    max_requests: int = 40,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, object]:
    """Replay cached responses on resume; save source/provenance after each query.

    The immutable discovery snapshot determines selection. An incomplete query is
    never labeled complete. HTTP failures stop; malformed records get safe reasons.
    """
    if not output.resolve().is_relative_to(Path("data/benchmarks/v1").resolve()):
        raise BenchmarkError("Acquisition output must be under data/benchmarks/v1/.")
    config = BenchmarkConfig()
    cases: dict[str, Case] = {}
    mappings: dict[str, OpinionMapping] = {}
    selections: list[QuerySelection] = []
    citations: list[CitationEvidence] = []
    rejected: list[dict[str, str]] = []
    status = "incomplete"
    failure: str | None = None
    selected_names: set[str] = set()
    if (output / "inputs.json").exists():
        if load_model(output / "config.json", BenchmarkConfig) != config:
            raise BenchmarkError(
                "Acquisition configuration changed; use a new directory."
            )
        saved = load_model(output / "inputs.json", BenchmarkInputs)
        if (output / "progress.json").exists():
            rejected = load_model(output / "progress.json", JobProgress).rejections
        cases = {c.source_id: c for c in read_cases(output / "sources.jsonl")}
        selections, citations = saved.selections, saved.citations
        mappings = {m.opinion_id: m for m in saved.opinion_mappings}
        for selection in selections:
            if selection.review_status != "review_required":
                raise BenchmarkError(
                    "Do not resume acquisition over human-reviewed inputs."
                )
            case = cases.get(selection.source_case_id)
            if case is None:
                raise BenchmarkError("Acquisition checkpoint is missing a query Case.")
            selected_names.add(" ".join(case.name.casefold().split()))
    session = Acquisition(
        token,
        output / "cache",
        interval=interval,
        timeout=timeout,
        max_requests=max_requests,
        transport=transport,
    )

    def reject(record: dict[str, str]) -> None:
        if record not in rejected:
            rejected.append(record)

    def save() -> None:
        atomic_write(output / "sources.jsonl", serialize_cases(list(cases.values())))
        used_ids = {key for evidence in citations for key in evidence.cited_opinion_ids}
        inputs = BenchmarkInputs(
            selections=selections,
            citations=citations,
            opinion_mappings=[mappings[key] for key in sorted(used_ids, key=int)],
            litigation_review_note=(
                "Automated exact-name deduplication only. Related litigation "
                "identification requires human review."
            ),
            source_provenance=(
                "CourtListener nonsemantic published Search discovery, "
                "field-selected REST details, paginated opinions-cited; response "
                "hashes retained in local cache."
            ),
        )
        atomic_write(output / "inputs.json", canonical(inputs) + "\n")
        atomic_write(output / "config.json", canonical(config) + "\n")
        atomic_write(
            output / "progress.json",
            json.dumps(
                {
                    "status": status,
                    "failure": failure,
                    "queries": len(selections),
                    "cases": len(cases),
                    "requests_this_run": session.requests,
                    "cache_hits": session.cache_hits,
                    "rejections": rejected,
                    "mapping_count": len(mappings),
                    "review_status": "review_required",
                },
                sort_keys=True,
                indent=2,
            ),
        )

    def resolve(oid: str) -> OpinionMapping:
        if oid in mappings:
            return mappings[oid]
        metadata, _, payload = session.opinion(oid)
        cluster = session.cluster(metadata.cluster_id)
        did = linked_id(cluster.docket, "dockets")
        docket = parse(
            session.get(f"dockets/{did}/", {"fields": "id,court_id,docket_number"}),
            DocketResponse,
        )
        if docket.id != did:
            raise IngestionError("Unexpected docket identifier.")
        mapping = OpinionMapping(
            opinion_id=oid,
            case_id=metadata.cluster_id,
            date_filed=cluster.date_filed,
            court=docket.court_id,
            case_name=cluster.case_name,
            reporter_citations=[
                " ".join(x for x in (c.volume, c.reporter, c.page) if x)
                for c in cluster.citations or []
            ],
            payload_sha256=digest(payload),
        )
        if (
            docket.court_id in config.candidate_courts
            and cluster.date_filed
            and cluster.date_filed <= config.candidate_cutoff
        ):
            cases[metadata.cluster_id] = session.case(metadata.cluster_id)
        mappings[oid] = mapping
        return mapping

    try:
        # Query quota first; do not infer membership or silently retry 429s.
        session.check_quota()
        query_ids = session.discover(
            "ca2", config.query_date_start, config.query_date_end
        )
        for identifier in query_ids:
            if len(selections) >= config.query_count:
                break
            if any(s.source_case_id == str(identifier) for s in selections):
                continue
            try:
                case = session.case(str(identifier))
                if (
                    case.court_id != "ca2"
                    or case.date_filed is None
                    or not config.query_date_start
                    <= case.date_filed
                    <= config.query_date_end
                ):
                    raise IngestionError(
                        "Authoritative query metadata is out of scope."
                    )
                name = " ".join(case.name.casefold().split())
                if name in selected_names:
                    reject(
                        {"case_id": str(identifier), "reason": "duplicate_case_name"}
                    )
                    continue
                lead = [
                    op for op in case.opinions if op.kind in {"010combined", "020lead"}
                ]
                if len(lead) != 1:
                    raise BenchmarkError("Lead/combined opinion is ambiguous.")
                op = lead[0]
                edges, hashes, _ = session.citations(op.source_id)
                ids = [resource_id(edge.cited_opinion, "opinions") for edge in edges]
                for oid in ids:
                    try:
                        resolve(oid)
                    except RequestFailure:
                        raise
                    except IngestionError:
                        mappings[oid] = OpinionMapping(
                            opinion_id=oid,
                            case_id=None,
                            payload_sha256=digest(
                                session.get(
                                    f"opinions/{oid}/", {"fields": OPINION_FIELDS}
                                )
                            ),
                            unresolved_reason="unavailable",
                        )
                        reject(
                            {"opinion_id": oid, "reason": "invalid_or_unusable_mapping"}
                        )
                positives = {
                    mapping.case_id
                    for oid in ids
                    for mapping in [mappings[oid]]
                    if mapping.case_id in cases
                    and mapping.case_id != case.source_id
                    and mapping.date_filed
                    and mapping.date_filed <= config.candidate_cutoff
                    and mapping.court in config.candidate_courts
                }
                if len(positives) < config.minimum_positives:
                    raise BenchmarkError("Fewer than two eligible positives.")
                terms = [case.name, *(case.reporter_citations or [])]
                for oid in ids:
                    terms.extend(
                        [
                            mappings[oid].case_name or "",
                            *mappings[oid].reporter_citations,
                        ]
                    )
                selection = propose_excerpt(case, op, terms)
                cases[case.source_id] = case
                selections.append(selection)
                selected_names.add(name)
                citations.append(
                    CitationEvidence(
                        citing_opinion_id=op.source_id,
                        cited_opinion_ids=ids,
                        relation_source="opinions-cited",
                        payload_sha256=digest("\n".join(hashes)),
                        page_sha256=hashes,
                        depths={
                            resource_id(e.cited_opinion, "opinions"): e.depth
                            for e in edges
                        },
                        complete=True,
                    )
                )
                save()
            except RequestFailure:
                raise
            except (IngestionError, BenchmarkError) as exc:
                rejected.append({"case_id": str(identifier), "reason": str(exc)})
                save()
        if len(selections) == config.query_count:
            # Freeze an objective metadata sampling frame before fetching distractors.
            # Ten-year windows keep discovery bounded; round-robin court/year in build.
            strata: dict[str, list[int]] = {}
            for court in config.candidate_courts:
                for year in range(
                    config.distractor_frame_year_start, config.candidate_cutoff.year + 1
                ):
                    frame_ids = session.discover(
                        court,
                        date(year, 1, 1),
                        date(year, 12, 31),
                        limit=config.distractor_frame_per_court_year,
                    )
                    strata[f"{court}:{year}"] = sorted(
                        frame_ids,
                        key=lambda key: digest(
                            f"{config.selection_seed}:{court}:{year}:{key}"
                        ),
                    )
            distractors = [
                ids[index]
                for index in range(config.distractor_frame_per_court_year)
                for _, ids in sorted(strata.items())
                if index < len(ids)
            ]
            for identifier in distractors:
                eligible = [
                    c
                    for c in cases.values()
                    if c.date_filed
                    and c.date_filed <= config.candidate_cutoff
                    and c.court_id in config.candidate_courts
                ]
                if len(eligible) >= config.candidate_target:
                    break
                if str(identifier) not in cases:
                    try:
                        candidate = session.case(str(identifier))
                        if (
                            candidate.court_id in config.candidate_courts
                            and candidate.date_filed
                            and candidate.date_filed <= config.candidate_cutoff
                        ):
                            cases[candidate.source_id] = candidate
                    except RequestFailure:
                        raise
                    except IngestionError:
                        reject(
                            {"case_id": str(identifier), "reason": "invalid_distractor"}
                        )
                    save()
            eligible_count = sum(
                c.court_id in config.candidate_courts
                and c.date_filed is not None
                and c.date_filed <= config.candidate_cutoff
                for c in cases.values()
            )
            if config.candidate_min <= eligible_count <= config.candidate_max:
                status = "ready_for_build"
            else:
                failure = "Candidate corpus is outside the required size bounds."
        else:
            failure = "Discovery exhausted before enough eligible excerpt candidates."
    except (IngestionError, BenchmarkError) as exc:
        failure = str(exc)
    finally:
        save()
        session.close()
    return {
        "status": status,
        "failure": failure,
        "queries": len(selections),
        "cases": len(cases),
        "requests": session.requests,
    }

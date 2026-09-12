"""Normalize opinion citation evidence into deterministic case-level edges."""

from collections import defaultdict

from lextrace.domain.case import Case
from lextrace.evaluation.benchmark import digest
from lextrace.graph.contracts import CitationEdge, OpinionCitation
from lextrace.graph.contracts import GraphEvidenceBundle as Bundle


def normalize_edges(
    cases: list[Case], bundle: Bundle
) -> tuple[list[CitationEdge], dict[str, int]]:
    opinion_cases = {
        opinion.source_id: case.source_id for case in cases for opinion in case.opinions
    }
    case_dates = {case.source_id: case.date_filed for case in cases}
    mappings = {mapping.opinion_id: mapping for mapping in bundle.opinion_mappings}
    grouped: dict[tuple[str, str], list[OpinionCitation]] = defaultdict(list)
    source_edges = mapped = unresolved = 0
    for evidence in sorted(
        bundle.citations, key=lambda item: int(item.citing_opinion_id)
    ):
        citing_case = opinion_cases.get(evidence.citing_opinion_id)
        mapped_citing = mappings.get(evidence.citing_opinion_id)
        if citing_case is None and mapped_citing is not None:
            citing_case = mapped_citing.case_id
        for cited_opinion in evidence.cited_opinion_ids:
            source_edges += 1
            cited_mapping = mappings.get(cited_opinion)
            if (
                citing_case is None
                or cited_mapping is None
                or cited_mapping.case_id is None
            ):
                unresolved += 1
                continue
            mapped += 1
            cited_case = cited_mapping.case_id
            provenance = digest(
                f"{evidence.payload_sha256}:{evidence.citing_opinion_id}:{cited_opinion}"
            )
            grouped[(citing_case, cited_case)].append(
                OpinionCitation(
                    citing_opinion_id=evidence.citing_opinion_id,
                    cited_opinion_id=cited_opinion,
                    citation_depth=evidence.depths.get(cited_opinion),
                    provenance_id=provenance,
                )
            )
            if cited_mapping.date_filed is not None:
                case_dates[cited_case] = cited_mapping.date_filed
    edges = []
    for (citing, cited), supports in sorted(
        grouped.items(), key=lambda item: (int(item[0][0]), int(item[0][1]))
    ):
        supports = sorted(
            {s.provenance_id: s for s in supports}.values(),
            key=lambda support: (
                int(support.citing_opinion_id),
                int(support.cited_opinion_id),
                support.provenance_id,
            ),
        )
        depths = [s.citation_depth for s in supports if s.citation_depth is not None]
        edges.append(
            CitationEdge(
                citing_case_id=citing,
                cited_case_id=cited,
                citing_case_date=case_dates.get(citing),
                cited_case_date=case_dates.get(cited),
                supports=supports,
                supporting_opinion_edge_count=len(supports),
                minimum_citation_depth=min(depths) if depths else None,
                maximum_citation_depth=max(depths) if depths else None,
                self_edge=citing == cited,
            )
        )
    return edges, {
        "source_edges": source_edges,
        "mapped_source_edges": mapped,
        "collapsed_duplicates": mapped - len(edges),
        "unresolved_mappings": unresolved,
    }

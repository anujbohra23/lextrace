"""Offline metrics on validated normalized Case records."""

import math
import statistics
from collections import Counter
from pathlib import Path

from lextrace.corpus import read_cases


def inspect_corpus(path: Path) -> dict[str, object]:
    cases = read_cases(path)
    ids = Counter(case.source_id for case in cases)
    lengths = sorted(len(opinion.text) for case in cases for opinion in case.opinions)
    dates = [case.date_filed for case in cases if case.date_filed is not None]
    return {
        "total_cases": len(cases),
        "reporter_citations": {
            "present": sum(bool(case.reporter_citations) for case in cases),
            "absent": sum(case.reporter_citations == [] for case in cases),
            "unknown": sum(case.reporter_citations is None for case in cases),
        },
        "docket_numbers": {
            "present": sum(case.docket_number is not None for case in cases),
            "absent": sum(case.docket_number is None for case in cases),
        },
        "duplicate_source_ids": sorted(key for key, count in ids.items() if count > 1),
        "duplicate_records": sum(count - 1 for count in ids.values()),
        "filing_date_range": {
            "min": min(dates).isoformat() if dates else None,
            "max": max(dates).isoformat() if dates else None,
        },
        "court_distribution": dict(
            sorted(Counter(case.court_id for case in cases).items())
        ),
        "opinion_text_lengths": {
            "count": len(lengths),
            "unit": "unicode_characters",
            "p95_method": "nearest_rank",
            "min": lengths[0] if lengths else None,
            "median": statistics.median(lengths) if lengths else None,
            "mean": statistics.mean(lengths) if lengths else None,
            "p95": lengths[math.ceil(0.95 * len(lengths)) - 1] if lengths else None,
            "max": lengths[-1] if lengths else None,
        },
    }

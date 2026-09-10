"""Metrics describe validated corpus records, not rejected source data."""

import json
from pathlib import Path

from lextrace.evaluation.corpus_quality import inspect_corpus


def test_quality_metrics(tmp_path: Path) -> None:
    records = []
    for identifier, length, citations, docket, court in [
        ("1", 2, ["1 F.3d 2"], "1", "ca2"),
        ("1", 4, [], None, "ca2"),
        ("2", 6, None, "2", "scotus"),
    ]:
        records.append(
            {
                "source_id": identifier,
                "source_url": "https://www.courtlistener.com/opinion/1/a/",
                "name": "A",
                "date_filed": "2020-01-01",
                "court_id": court,
                "docket_number": docket,
                "reporter_citations": citations,
                "opinions": [
                    {
                        "source_id": "3",
                        "kind": "unknown",
                        "text": "x" * length,
                        "text_source_field": "plain_text",
                    }
                ],
            }
        )
    path = tmp_path / "cases.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    metrics = inspect_corpus(path)
    assert metrics["total_cases"] == 3
    assert metrics["reporter_citations"] == {"present": 1, "absent": 1, "unknown": 1}
    assert metrics["docket_numbers"] == {"present": 2, "absent": 1}
    assert metrics["duplicate_source_ids"] == ["1"]
    assert metrics["duplicate_records"] == 1
    assert metrics["court_distribution"] == {"ca2": 2, "scotus": 1}
    assert metrics["opinion_text_lengths"] == {
        "count": 3,
        "unit": "unicode_characters",
        "p95_method": "nearest_rank",
        "min": 2,
        "median": 4,
        "mean": 4,
        "p95": 6,
        "max": 6,
    }
    assert "missing_text" not in metrics
    assert "source_records_encountered" not in metrics


def test_empty_corpus(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_text("")
    result = inspect_corpus(path)
    assert result["total_cases"] == 0
    assert result["filing_date_range"] == {"min": None, "max": None}
    assert isinstance(result["opinion_text_lengths"], dict)
    assert result["opinion_text_lengths"]["p95"] is None

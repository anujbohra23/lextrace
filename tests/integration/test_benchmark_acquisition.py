"""Exercise the full acquisition workflow with synthetic HTTPX responses."""

import json
from pathlib import Path

import httpx
import pytest

from lextrace.domain.case import Case
from lextrace.evaluation.benchmark import BenchmarkConfig, BenchmarkInputs
from lextrace.evaluation.benchmark_build import assemble
from lextrace.ingestion.benchmark_job import acquire_benchmark

Sample = tuple[list[Case], BenchmarkInputs, BenchmarkConfig]
BASE = "https://www.courtlistener.com/api/rest/v4/"


@pytest.mark.parametrize("malformed_first", [False, True])
def test_complete_job_and_resume(
    benchmark_sample: Sample,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    malformed_first: bool,
) -> None:
    monkeypatch.chdir(tmp_path)
    cases, _, config = benchmark_sample
    from datetime import date

    extra = cases[-1].model_copy(
        update={
            "source_id": "1026",
            "name": "Query 26 v. Agency",
            "date_filed": date(2010, 1, 26),
            "opinions": [
                cases[-1].opinions[0].model_copy(update={"source_id": "2026"})
            ],
        }
    )
    cases = [*cases, extra]
    by_case = {c.source_id: c for c in cases}
    by_op = {op.source_id: (case, op) for case in cases for op in case.opinions}
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        resource = req.url.path.removeprefix("/api/rest/v4/").split("/")[0]
        if resource == "api-usage":
            return httpx.Response(
                200,
                json={
                    "current_usage": [
                        {
                            "scope": "user",
                            "remaining": 3000,
                            "blocked": False,
                            "window_seconds": 86400,
                        }
                    ]
                },
            )
        if resource == "search":
            params = req.url.params
            found = [
                case
                for case in cases
                if case.court_id == params["court"]
                and case.date_filed is not None
                and params["filed_after"]
                <= str(case.date_filed)
                <= params["filed_before"]
            ]
            return httpx.Response(
                200,
                json={
                    "next": None,
                    "results": [
                        {
                            "cluster_id": int(c.source_id),
                            "caseName": c.name,
                            "court_id": c.court_id,
                            "dateFiled": str(c.date_filed),
                            "opinions": [
                                {"id": int(op.source_id), "type": "combined-opinion"}
                                for op in c.opinions
                            ],
                        }
                        for c in found
                    ],
                },
            )
        if resource == "opinions-cited":
            return httpx.Response(
                200,
                json={
                    "next": None,
                    "results": [
                        {
                            "citing_opinion": BASE
                            + "opinions/"
                            + req.url.params["citing_opinion"]
                            + "/",
                            "cited_opinion": BASE + f"opinions/{oid}/",
                            "depth": 1,
                        }
                        for oid in ["10001", "10002"]
                    ],
                },
            )
        identifier = req.url.path.strip("/").split("/")[-1]
        if resource == "opinions":
            case, op = by_op[identifier]
            return httpx.Response(
                200,
                json={
                    "id": int(identifier),
                    "cluster": BASE + f"clusters/{case.source_id}/",
                    "type": op.kind,
                    "opinions_cited": [],
                    "plain_text": ""
                    if malformed_first and identifier == "2001"
                    else op.text,
                },
            )
        case = by_case[identifier]
        if resource == "clusters":
            return httpx.Response(
                200,
                json={
                    "id": int(identifier),
                    "case_name": case.name,
                    "date_filed": str(case.date_filed),
                    "absolute_url": f"/opinion/{identifier}/example/",
                    "docket": BASE + f"dockets/{identifier}/",
                    "sub_opinions": [
                        BASE + f"opinions/{o.source_id}/" for o in case.opinions
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "id": int(identifier),
                "court_id": case.court_id,
                "docket_number": case.docket_number,
            },
        )

    path = Path("data/benchmarks/v1/acquire")
    result = acquire_benchmark(
        path,
        "test-secret",
        interval=0.000001,
        max_requests=2000,
        transport=httpx.MockTransport(handler),
    )
    assert result["status"] == "ready_for_build", result
    assert result["queries"] == 25
    if malformed_first:
        assert json.loads((path / "progress.json").read_text())["rejections"]
    # All emitted inputs are valid and can build a provisional V1 bundle.
    from lextrace.corpus import read_cases

    inputs = BenchmarkInputs.model_validate_json((path / "inputs.json").read_bytes())
    artifact = assemble(read_cases(path / "sources.jsonl"), inputs, config)
    assert json.loads(artifact["manifest.json"])["review_status"] == "review_required"
    original = (path / "inputs.json").read_bytes()
    calls.clear()
    resumed = acquire_benchmark(
        path,
        "test-secret",
        interval=0.000001,
        max_requests=2000,
        transport=httpx.MockTransport(handler),
    )
    assert resumed["status"] == "ready_for_build"
    assert (path / "inputs.json").read_bytes() == original
    assert calls == ["/api/rest/v4/api-usage/"]

    # A later exhausted quota must never erase completed canonical input files.
    source_bytes = (path / "sources.jsonl").read_bytes()
    failed = acquire_benchmark(
        path,
        "test-secret",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={
                    "current_usage": [
                        {
                            "scope": "user",
                            "remaining": 0,
                            "blocked": True,
                            "window_seconds": 3600,
                        }
                    ]
                },
            )
        ),
    )
    assert failed["status"] == "incomplete" and failed["queries"] == 25
    assert (path / "inputs.json").read_bytes() == original
    assert (path / "sources.jsonl").read_bytes() == source_bytes

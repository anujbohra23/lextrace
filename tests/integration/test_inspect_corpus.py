"""CLI corpus commands, including offline inspection and safe failures."""

import json
from pathlib import Path

import httpx
import pytest

from lextrace.cli import main


def test_corpus_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COURTLISTENER_API_TOKEN", "synthetic-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("clusters/"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": 1,
                            "case_name": "A",
                            "absolute_url": "/opinion/1/a/",
                            "docket": "/api/rest/v4/dockets/2/",
                            "sub_opinions": ["/api/rest/v4/opinions/3/"],
                            "citations": [],
                        }
                    ],
                    "next": None,
                },
            )
        if request.url.path.endswith("dockets/2/"):
            return httpx.Response(200, json={"id": 2, "court_id": "ca2"})
        return httpx.Response(200, json={"id": 3, "plain_text": "Text"})

    main(
        [
            "ingest-corpus",
            "--court",
            "ca2",
            "--max-cases",
            "1",
            "--output",
            "data/sample.jsonl",
            "--request-interval",
            "0.000001",
        ],
        transport=httpx.MockTransport(handler),
    )
    result = capsys.readouterr()
    assert result.err == ""
    assert json.loads(result.out)["status"] == "complete"
    monkeypatch.delenv("COURTLISTENER_API_TOKEN")
    main(["inspect-corpus", "data/sample.jsonl"])
    result = capsys.readouterr()
    assert result.err == ""
    assert json.loads(result.out)["reporter_citations"] == {
        "present": 0,
        "absent": 1,
        "unknown": 0,
    }


@pytest.mark.parametrize(
    "extra",
    [
        ["--request-interval", "nan"],
        ["--request-interval", "0"],
        ["--filed-after", "2022-01-01", "--filed-before", "2020-01-01"],
        ["--court", "invalid/court"],
    ],
)
def test_usage_errors(extra: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(
            [
                "ingest-corpus",
                "--court",
                "ca2",
                "--max-cases",
                "1",
                "--output",
                "data/a.jsonl",
                "--request-interval",
                "15",
                *extra,
            ]
        )
    assert error.value.code == 2
    assert capsys.readouterr().out == ""


def test_inspect_malformed_fails_safely(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text('{"private": "synthetic-secret"}\n')
    with pytest.raises(SystemExit) as error:
        main(["inspect-corpus", str(path)])
    assert error.value.code == 1
    output = capsys.readouterr()
    assert "line 1" in output.err
    assert "synthetic-secret" not in output.err
    assert output.out == ""

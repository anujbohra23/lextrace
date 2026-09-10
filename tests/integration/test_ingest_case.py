"""CLI integration with mocked HTTP transport only."""

import json
from pathlib import Path

import httpx
import pytest

from lextrace.cli import main
from lextrace.domain.case import Case

TOKEN = "test-secret-must-never-appear"


def test_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("COURTLISTENER_API_TOKEN", TOKEN)
    data = json.loads(
        (Path(__file__).parents[1] / "fixtures/courtlistener_case.json").read_text()
    )
    responses = [data["cluster"], data["docket"], *data["opinions"]]
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "www.courtlistener.com"
        assert request.headers["Authorization"] == f"Token {TOKEN}"
        assert request.extensions["timeout"]["read"] == 30.0
        paths.append(request.url.path)
        return httpx.Response(200, json=responses[len(paths) - 1])

    main(["ingest-case", "1"], transport=httpx.MockTransport(handler))
    output = capsys.readouterr()
    case = Case.model_validate_json(output.out)
    assert len(case.opinions) == 2
    assert output.err == ""
    assert '\n  "source"' in output.out
    assert TOKEN not in output.out
    assert paths == [
        "/api/rest/v4/clusters/1/",
        "/api/rest/v4/dockets/2/",
        "/api/rest/v4/opinions/3/",
        "/api/rest/v4/opinions/4/",
    ]


@pytest.mark.parametrize(
    "failure",
    [
        "401",
        "403",
        "404",
        "429",
        "500",
        "503",
        "302",
        "timeout",
        "network",
        "json",
        "schema",
        "wrong-id",
    ],
)
def test_failures(
    failure: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("COURTLISTENER_API_TOKEN", TOKEN)

    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "timeout":
            raise httpx.ReadTimeout(TOKEN, request=request)
        if failure == "network":
            raise httpx.ConnectError(TOKEN, request=request)
        if failure == "json":
            return httpx.Response(200, text=TOKEN)
        if failure == "schema":
            return httpx.Response(200, json={"id": TOKEN})
        if failure == "wrong-id":
            return httpx.Response(
                200,
                json={
                    "id": 99,
                    "absolute_url": "/opinion/99/a/",
                    "case_name": "A",
                    "docket": "/api/rest/v4/dockets/2/",
                    "sub_opinions": ["/api/rest/v4/opinions/3/"],
                },
            )
        return httpx.Response(
            int(failure), text=TOKEN, headers={"Location": "https://evil.test/"}
        )

    with pytest.raises(SystemExit) as error:
        main(["ingest-case", "1"], transport=httpx.MockTransport(handler))
    assert error.value.code == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.startswith("Error:")
    assert TOKEN not in output.err


@pytest.mark.parametrize(
    "identifier", ["0", "-1", "abc", "1.2", "https://example.com/1", "١"]
)
def test_invalid_id(identifier: str, capsys: pytest.CaptureFixture[str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid IDs must not make requests")

    with pytest.raises(SystemExit) as error:
        main(["ingest-case", identifier], transport=httpx.MockTransport(handler))
    assert error.value.code == 2
    assert "positive integer" in capsys.readouterr().err


@pytest.mark.parametrize("token", [None, " ", "secret\ninvalid", "sécret"])
def test_missing_or_invalid_token(
    token: str | None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("COURTLISTENER_API_TOKEN", raising=False)
    if token is not None:
        monkeypatch.setenv("COURTLISTENER_API_TOKEN", token)

    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid credentials must not make requests")

    with pytest.raises(SystemExit) as error:
        main(["ingest-case", "1"], transport=httpx.MockTransport(handler))
    assert error.value.code == 1
    output = capsys.readouterr()
    assert "COURTLISTENER_API_TOKEN" in output.err
    assert "secret" not in output.err


def test_help_without_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("COURTLISTENER_API_TOKEN", raising=False)
    main([])
    assert "ingest-case" in capsys.readouterr().out


@pytest.mark.parametrize("failure", ["missing-text", "opinion-server-error"])
def test_late_failure_has_no_partial_output(
    failure: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("COURTLISTENER_API_TOKEN", TOKEN)
    data = json.loads(
        (Path(__file__).parents[1] / "fixtures/courtlistener_case.json").read_text()
    )
    responses = [data["cluster"], data["docket"], *data["opinions"]]
    responses[-1]["plain_text"] = None
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 4 and failure == "opinion-server-error":
            return httpx.Response(503, text=TOKEN)
        return httpx.Response(200, json=responses[calls - 1])

    with pytest.raises(SystemExit) as error:
        main(["ingest-case", "1"], transport=httpx.MockTransport(handler))
    assert error.value.code == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert TOKEN not in output.err
    assert "Error:" in output.err


@pytest.mark.parametrize(
    "stage", ["courtlistener_token", "fetch_case", "normalize_case"]
)
def test_unexpected_value_error_is_not_rendered(
    stage: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import lextrace.cli as cli

    monkeypatch.setenv("COURTLISTENER_API_TOKEN", TOKEN)
    data = json.loads(
        (Path(__file__).parents[1] / "fixtures/courtlistener_case.json").read_text()
    )
    responses = iter([data["cluster"], data["docket"], *data["opinions"]])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(responses))

    def unexpected(*args: object, **kwargs: object) -> None:
        raise ValueError(f"Authorization: Token {TOKEN}; private response body")

    monkeypatch.setattr(cli, stage, unexpected)
    with pytest.raises(ValueError):
        main(["ingest-case", "1"], transport=httpx.MockTransport(handler))
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == ""

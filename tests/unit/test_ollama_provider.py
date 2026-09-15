"""Offline checks for the local StructuredLLM adapter."""

import json
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel

from lextrace.config import AppSettings
from lextrace.research.contracts import ResearchError
from lextrace.research.llm import configured_llm
from lextrace.research.ollama import OllamaLLM
from lextrace.research.prompts import PromptDefinition
from lextrace.research.runtime import CachedStructuredLLM, StructuredCache


class Issue(BaseModel):
    label: str


PROMPT = PromptDefinition(
    prompt_id="matter-issues",
    input_schema="synthetic text",
    output_schema="Issue",
    description="test",
    instructions="Return only an issue label from the supplied text.",
)


def _response(content: str, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "message": {"content": content},
            "prompt_eval_count": 12,
            "eval_count": 5,
            "load_duration": 2_000_000,
        },
    )


def test_structured_request_and_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        assert "authorization" not in request.headers
        payload = json.loads(request.content)
        assert payload["format"] == Issue.model_json_schema()
        assert payload["think"] is False
        assert payload["stream"] is False
        assert payload["options"]["temperature"] == 0
        assert payload["options"]["num_ctx"] == 8192
        assert payload["model"] == "qwen3:8b"
        return _response('{"label":"synthetic issue"}')

    llm = OllamaLLM("qwen3:8b", transport=httpx.MockTransport(handler))
    result = llm.generate(PROMPT, Issue, {"text": "synthetic"})
    assert result.label == "synthetic issue"
    assert llm.provider == "ollama" and llm.model == "qwen3:8b"
    assert llm.usage.calls == 1
    assert (llm.usage.input_tokens, llm.usage.output_tokens) == (12, 5)
    assert llm.last_load_ms == 2
    llm.close()


@pytest.mark.parametrize(
    "response,expected",
    [
        (_response("not json"), "malformed"),
        (_response('{"wrong":"field"}'), "malformed"),
        (_response(""), "no structured"),
        (httpx.Response(403, text="SECRET provider body"), "generation failed"),
    ],
)
def test_bad_output_is_safe(response: httpx.Response, expected: str) -> None:
    llm = OllamaLLM("qwen3:8b", transport=httpx.MockTransport(lambda _: response))
    with pytest.raises(ResearchError, match=expected) as captured:
        llm.generate(PROMPT, Issue, {"text": "synthetic"})
    assert "SECRET" not in str(captured.value)
    llm.close()


def test_transient_retry_is_bounded() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503) if calls == 1 else _response('{"label":"ok"}')

    llm = OllamaLLM("qwen3:8b", max_retries=1, transport=httpx.MockTransport(handler))
    assert llm.generate(PROMPT, Issue, {}).label == "ok"
    assert llm.retries == 1 and llm.usage.calls == 2
    llm.close()


@pytest.mark.parametrize(
    "error", [httpx.ConnectError("SECRET"), httpx.ReadTimeout("SECRET")]
)
def test_network_failure_is_safe(error: Exception) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise error

    llm = OllamaLLM("qwen3:8b", max_retries=0, transport=httpx.MockTransport(handler))
    with pytest.raises(ResearchError, match="unavailable") as captured:
        llm.generate(PROMPT, Issue, {})
    assert "SECRET" not in str(captured.value)
    llm.close()


def test_configuration_and_cache_identity(tmp_path: Path) -> None:
    with pytest.raises(ResearchError, match="configuration"):
        OllamaLLM("qwen3:8b", base_url="https://outside.example")
    settings = AppSettings(llm_provider="ollama", llm_model="qwen3:8b")
    assert settings.llm_provider == "ollama"
    selected = configured_llm("qwen3:8b", provider="ollama")
    assert selected.provider == "ollama"
    assert selected.model == "qwen3:8b"
    assert isinstance(selected, OllamaLLM)
    selected.close()

    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response('{"label":"ok"}')

    cache = StructuredCache(tmp_path / "cache.sqlite3")
    first = CachedStructuredLLM(
        OllamaLLM("qwen3:8b", transport=httpx.MockTransport(handler)), cache
    )
    second = CachedStructuredLLM(
        OllamaLLM("qwen3:other", transport=httpx.MockTransport(handler)), cache
    )
    other_provider = CachedStructuredLLM(
        OllamaLLM("qwen3:8b", transport=httpx.MockTransport(handler)), cache
    )
    other_provider.provider = "openai-compatible"
    first.generate(PROMPT, Issue, {})
    first.generate(PROMPT, Issue, {})
    second.generate(PROMPT, Issue, {})
    other_provider.generate(PROMPT, Issue, {})
    assert calls == 3
    assert first.hits == 1 and second.hits == 0
    assert other_provider.hits == 0
    cache.close()

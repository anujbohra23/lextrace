"""Local Ollama implementation of the existing structured LLM contract."""

import json
import time
from typing import TypeVar
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ValidationError

from lextrace.research.contracts import ResearchError, Usage
from lextrace.research.prompts import PromptDefinition

Output = TypeVar("Output", bound=BaseModel)
TRANSIENT_STATUS = {429, 500, 502, 503, 504}


class OllamaLLM:
    """Schema-constrained local chat; never expose provider bodies or prompts."""

    provider = "ollama"

    def __init__(
        self,
        model: str,
        *,
        base_url: str = "http://127.0.0.1:11434",
        timeout: float = 180,
        max_retries: int = 2,
        max_completion_tokens: int = 4096,
        context_tokens: int = 8192,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        try:
            parsed = urlsplit(base_url)
            port = parsed.port
        except ValueError:
            raise ResearchError("Local Ollama configuration is invalid.") from None
        if (
            not model.strip()
            or parsed.scheme != "http"
            or parsed.hostname not in {"localhost", "127.0.0.1", "host.docker.internal"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or port is None
            and ":" in (parsed.netloc.split("]")[-1])
            or timeout <= 0
            or max_retries < 0
            or max_retries > 3
            or max_completion_tokens < 1
            or context_tokens < 256
        ):
            raise ResearchError("Local Ollama configuration is invalid.")
        self.model = model
        self.retries = 0
        self.usage = Usage()
        self.total_latency_ms = 0
        self.last_load_ms = 0
        self._max_retries = max_retries
        self._max_completion_tokens = max_completion_tokens
        self._context_tokens = context_tokens
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
        )

    def generate(
        self,
        prompt: PromptDefinition,
        schema: type[Output],
        context: dict[str, object],
    ) -> Output:
        payload = {
            "model": self.model,
            "stream": False,
            "think": False,
            "format": schema.model_json_schema(),
            "options": {
                "temperature": 0,
                "num_ctx": self._context_tokens,
                "num_predict": self._max_completion_tokens,
            },
            "messages": [
                {"role": "system", "content": prompt.instructions},
                {
                    "role": "user",
                    "content": '<RETRIEVED_EVIDENCE untrusted="true">\n'
                    + json.dumps(context, ensure_ascii=False, sort_keys=True)
                    + "\n</RETRIEVED_EVIDENCE>",
                },
            ],
        }
        for attempt in range(self._max_retries + 1):
            started = time.monotonic()
            self.usage.calls += 1
            try:
                response = self._client.post("/api/chat", json=payload)
                if response.status_code in TRANSIENT_STATUS:
                    if attempt < self._max_retries:
                        self.retries += 1
                        time.sleep(min(2**attempt, 2))
                        continue
                    raise ResearchError("Local Ollama generation failed.")
                if response.status_code != 200:
                    raise ResearchError("Local Ollama generation failed.")
                body = response.json()
                if not isinstance(body, dict):
                    raise ResearchError("Ollama returned malformed structured output.")
                message = body.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                if not isinstance(content, str) or not content.strip():
                    raise ResearchError("Ollama returned no structured output.")
                result = schema.model_validate_json(content)
                self.usage.input_tokens += _nonnegative_int(
                    body.get("prompt_eval_count")
                )
                self.usage.output_tokens += _nonnegative_int(body.get("eval_count"))
                self.total_latency_ms += round((time.monotonic() - started) * 1000)
                self.last_load_ms = (
                    _nonnegative_int(body.get("load_duration")) // 1_000_000
                )
                return result
            except (ValidationError, ValueError, TypeError):
                raise ResearchError(
                    "Ollama returned malformed structured output."
                ) from None
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt == self._max_retries:
                    raise ResearchError("Local Ollama is unavailable.") from None
                self.retries += 1
                time.sleep(min(2**attempt, 2))
        raise ResearchError("Local Ollama generation failed.")

    def close(self) -> None:
        self._client.close()


def _nonnegative_int(value: object) -> int:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else 0
    )

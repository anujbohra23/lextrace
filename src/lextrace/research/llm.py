"""Provider-independent structured generation with a thin OpenAI adapter."""

import json
import time
from typing import Any, Protocol, TypeVar, cast

from pydantic import BaseModel, ValidationError

from lextrace.research.contracts import ResearchError, Usage
from lextrace.research.prompts import PromptDefinition

Output = TypeVar("Output", bound=BaseModel)


class StructuredLLM(Protocol):
    provider: str
    model: str
    retries: int
    usage: Usage

    def generate(
        self,
        prompt: PromptDefinition,
        schema: type[Output],
        context: dict[str, object],
    ) -> Output: ...


class OpenAICompatibleLLM:
    provider = "openai-compatible"

    def __init__(
        self,
        model: str,
        *,
        api_key: str,
        base_url: str | None = None,
        timeout: float = 60,
        max_retries: int = 2,
    ) -> None:
        if not api_key:
            raise ResearchError("LLM API credential is missing.")
        self.model = model
        self.retries = 0
        self.usage = Usage()
        try:
            from openai import (
                APIConnectionError,
                APITimeoutError,
                InternalServerError,
                OpenAI,
                RateLimitError,
            )

            self._client = OpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout,
                max_retries=0,
            )
            self._transient_errors: tuple[type[Exception], ...] = (
                APIConnectionError,
                APITimeoutError,
                InternalServerError,
                RateLimitError,
            )
        except Exception:
            raise ResearchError(
                "Could not initialize the configured LLM provider."
            ) from None
        self.max_retries = max_retries

    def generate(
        self,
        prompt: PromptDefinition,
        schema: type[Output],
        context: dict[str, object],
    ) -> Output:
        payload = json.dumps(context, ensure_ascii=False, sort_keys=True)
        messages = [
            {"role": "system", "content": prompt.instructions},
            {
                "role": "user",
                "content": '<RETRIEVED_EVIDENCE untrusted="true">\n'
                + payload
                + "\n</RETRIEVED_EVIDENCE>",
            },
        ]
        for attempt in range(self.max_retries + 1):
            try:
                self.usage.calls += 1
                response = self._client.chat.completions.parse(
                    model=self.model,
                    messages=cast(Any, messages),
                    response_format=schema,
                    temperature=0,
                )
                parsed = response.choices[0].message.parsed
                if parsed is None:
                    raise ResearchError("LLM returned no structured output.")
                usage = response.usage
                if usage is not None:
                    self.usage.input_tokens += usage.prompt_tokens
                    self.usage.output_tokens += usage.completion_tokens
                return schema.model_validate(parsed)
            except ResearchError:
                raise
            except ValidationError:
                raise ResearchError(
                    "LLM returned malformed structured output."
                ) from None
            except Exception as error:
                if attempt == self.max_retries or not isinstance(
                    error, self._transient_errors
                ):
                    raise ResearchError("Structured LLM generation failed.") from None
                self.retries += 1
                time.sleep(min(2**attempt, 2))
        raise ResearchError("Structured LLM generation failed.")

"""Validated environment configuration; credentials remain lazy and unlogged."""

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

APP_TITLE = "LexTrace"
COURTLISTENER_API_BASE_URL = "https://www.courtlistener.com/api/rest/v4/"
COURTLISTENER_TIMEOUT = 30.0


class ConfigurationError(Exception):
    """A safe configuration message containing no supplied values."""


class AppSettings(BaseModel):
    """Production runtime settings loaded from one environment boundary."""

    model_config = ConfigDict(extra="forbid")

    index_path: Path = Path("artifacts/indexes/default")
    graph_path: Path | None = None
    runtime_db: Path = Path("artifacts/runtime/lextrace.sqlite3")
    cache_db: Path = Path("artifacts/runtime/cache.sqlite3")
    llm_model: str | None = None
    llm_base_url: str | None = None
    persist_content: bool = True
    max_concurrent_research: int = Field(default=1, ge=1, le=8)
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    @classmethod
    def from_environment(cls) -> "AppSettings":
        graph = os.environ.get("LEXTRACE_GRAPH_PATH")
        origins = os.environ.get("LEXTRACE_CORS_ORIGINS", "http://localhost:3000")
        try:
            return cls(
                index_path=Path(
                    os.environ.get("LEXTRACE_INDEX_PATH", "artifacts/indexes/default")
                ),
                graph_path=Path(graph) if graph else None,
                runtime_db=Path(
                    os.environ.get(
                        "LEXTRACE_RUNTIME_DB", "artifacts/runtime/lextrace.sqlite3"
                    )
                ),
                cache_db=Path(
                    os.environ.get(
                        "LEXTRACE_CACHE_DB", "artifacts/runtime/cache.sqlite3"
                    )
                ),
                llm_model=os.environ.get("LEXTRACE_LLM_MODEL"),
                llm_base_url=os.environ.get("OPENAI_BASE_URL"),
                persist_content=os.environ.get(
                    "LEXTRACE_PERSIST_CONTENT", "true"
                ).lower()
                in {"1", "true", "yes"},
                max_concurrent_research=int(
                    os.environ.get("LEXTRACE_MAX_CONCURRENT_RESEARCH", "1")
                ),
                cors_origins=[
                    origin.strip() for origin in origins.split(",") if origin.strip()
                ],
            )
        except (ValueError, TypeError):
            raise ConfigurationError(
                "LexTrace runtime configuration is invalid."
            ) from None


def courtlistener_token() -> str:
    """Read credentials lazily, without affecting CLI help or the health API."""
    token = os.environ.get("COURTLISTENER_API_TOKEN", "").strip()
    if not token:
        raise ConfigurationError("Set COURTLISTENER_API_TOKEN before ingesting a case.")
    if not token.isascii() or any(
        char.isspace() or ord(char) < 32 or ord(char) == 127 for char in token
    ):
        raise ConfigurationError("COURTLISTENER_API_TOKEN has an invalid format.")
    return token


def openai_api_key() -> str:
    """Read the LLM credential only when a research runtime is requested."""
    token = os.environ.get("OPENAI_API_KEY", "").strip()
    if not token:
        raise ConfigurationError("Set OPENAI_API_KEY before running research.")
    if any(ord(char) < 32 or ord(char) == 127 for char in token):
        raise ConfigurationError("OPENAI_API_KEY has an invalid format.")
    return token

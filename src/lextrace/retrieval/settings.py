"""Versioned local-engine configuration."""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, ValidationError, model_validator

from lextrace.retrieval.contracts import Mode, Positive, Record, RetrievalError


class LexicalSettings(Record):
    k1: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 1.2
    b: Annotated[float, Field(ge=0, le=1)] = 0.75


class PassageSettings(Record):
    words: Annotated[int, Field(strict=True, ge=16, le=256)] = 128
    evidence_count: Annotated[int, Field(strict=True, ge=1, le=3)] = 2


class DenseSettings(Record):
    model: str = "sentence-transformers/all-MiniLM-L6-v2"
    revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")] = (
        "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
    )
    batch_size: Positive = 32
    block_size: Positive = 4096
    window_tokens: Annotated[int, Field(strict=True, ge=16, le=256)] = 256
    window_overlap_tokens: Annotated[int, Field(strict=True, ge=0, le=64)] = 32
    device: Literal["cpu", "mps"] = "cpu"

    @model_validator(mode="after")
    def validate_window(self) -> "DenseSettings":
        if self.window_overlap_tokens >= self.window_tokens - 2:
            raise ValueError("Token-window overlap must leave room for content.")
        return self


class RerankerSettings(Record):
    model: str = "cross-encoder/ms-marco-MiniLM-L6-v2"
    revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")] = (
        "233902d25c440f23af6f7d6e94d2946bac0bee0a"
    )
    batch_size: Positive = 16
    device: Literal["cpu", "mps"] = "cpu"
    candidate_depth: Positive = 30


class HybridSettings(Record):
    lexical_depth: Positive = 100
    dense_depth: Positive = 100
    rrf_constant: Positive = 60
    candidate_depth: Positive = 50


class EngineConfig(Record):
    format_version: Literal[1] = 1
    default_mode: Mode = "reranked"
    bm25: LexicalSettings = Field(default_factory=LexicalSettings)
    dense: DenseSettings = Field(default_factory=DenseSettings)
    hybrid: HybridSettings = Field(default_factory=HybridSettings)
    reranker: RerankerSettings = Field(default_factory=RerankerSettings)
    passages: PassageSettings = Field(default_factory=PassageSettings)
    preprocessing: Literal["all-opinions-stored-order-v1"] = (
        "all-opinions-stored-order-v1"
    )


def read_config(path: Path | None = None) -> EngineConfig:
    if path is None:
        return EngineConfig()
    try:
        return EngineConfig.model_validate_json(path.read_bytes())
    except (OSError, ValidationError):
        raise RetrievalError(
            "Could not read a valid retrieval configuration."
        ) from None

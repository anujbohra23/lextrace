"""Lazy, pinned local model adapters. Tests inject small deterministic protocols."""

from collections.abc import Sequence
from typing import Any, Protocol

import numpy as np
from numpy.typing import NDArray

from lextrace.retrieval.contracts import RetrievalError
from lextrace.retrieval.settings import DenseSettings, RerankerSettings

Vector = NDArray[np.float32]


class Encoder(Protocol):
    def encode(self, texts: Sequence[str]) -> Vector: ...


class PairScorer(Protocol):
    def score(self, pairs: Sequence[tuple[str, str]]) -> list[float]: ...


def unit(matrix: Vector) -> Vector:
    if matrix.ndim != 2 or not np.isfinite(matrix).all():
        raise RetrievalError("Encoder returned invalid vectors.")
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    if np.any(norms <= 0):
        raise RetrievalError("Encoder returned an empty vector.")
    return np.asarray(matrix / norms, dtype=np.float32)


class SentenceEncoder:
    def __init__(self, settings: DenseSettings) -> None:
        self.settings = settings
        self.device = settings.device
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is None:
            try:
                import torch
                from sentence_transformers import SentenceTransformer

                if self.device == "mps" and not torch.backends.mps.is_available():
                    self.device = "cpu"

                self._model = SentenceTransformer(
                    self.settings.model,
                    revision=self.settings.revision,
                    device=self.device,
                    cache_folder="artifacts/models",
                    trust_remote_code=False,
                    token=False,
                    model_kwargs={"use_safetensors": True},
                )
                self._model.eval()
            except Exception:
                raise RetrievalError(
                    "Could not load embedding model; install retrieval extras and "
                    "cache the pinned model. CPU is the supported fallback."
                ) from None
        return self._model

    def encode(self, texts: Sequence[str]) -> Vector:
        """Pool all token windows instead of silently truncating long text."""
        try:
            model = self._load()
            chunks: list[str] = []
            ranges: list[tuple[int, int]] = []
            width = int(model.max_seq_length) - 2
            for text in texts:
                tokens = model.tokenizer.encode(text, add_special_tokens=False)
                if not tokens:
                    raise RetrievalError("Text has no model tokens.")
                start = len(chunks)
                chunks.extend(
                    model.tokenizer.decode(
                        tokens[i : i + width], skip_special_tokens=True
                    )
                    for i in range(0, len(tokens), width)
                )
                ranges.append((start, len(chunks)))
            encoded = np.asarray(
                model.encode(
                    chunks,
                    batch_size=self.settings.batch_size,
                    show_progress_bar=False,
                    convert_to_numpy=True,
                    normalize_embeddings=True,
                ),
                dtype=np.float32,
            )
            return unit(
                np.asarray(
                    [encoded[first:last].mean(axis=0) for first, last in ranges],
                    dtype=np.float32,
                )
            )
        except RetrievalError:
            raise
        except Exception:
            raise RetrievalError(
                "Embedding inference failed; no results were substituted. "
                "Try CPU if using MPS."
            ) from None

    def close(self) -> None:
        self._model = None


class CrossScorer:
    def __init__(self, settings: RerankerSettings) -> None:
        self.settings = settings
        self.device = settings.device
        self._model: Any = None

    def score(self, pairs: Sequence[tuple[str, str]]) -> list[float]:
        try:
            if self._model is None:
                import torch
                from sentence_transformers import CrossEncoder

                if self.device == "mps" and not torch.backends.mps.is_available():
                    self.device = "cpu"
                from torch.nn import Identity

                self._model = CrossEncoder(
                    self.settings.model,
                    revision=self.settings.revision,
                    device=self.device,
                    cache_folder="artifacts/models",
                    trust_remote_code=False,
                    token=False,
                    max_length=512,
                    activation_fn=Identity(),
                    model_kwargs={"use_safetensors": True},
                )
            values = np.asarray(
                self._model.predict(
                    list(pairs),
                    batch_size=self.settings.batch_size,
                    show_progress_bar=False,
                ),
                dtype=np.float32,
            ).reshape(-1)
            if len(values) != len(pairs) or not np.isfinite(values).all():
                raise RetrievalError("Reranker returned invalid scores.")
            return [float(v) for v in values]
        except RetrievalError:
            raise
        except Exception:
            raise RetrievalError(
                "Cross-encoder inference failed; no results were substituted. "
                "Try CPU if using MPS."
            ) from None

    def close(self) -> None:
        self._model = None

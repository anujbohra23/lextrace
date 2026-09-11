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
            import torch

            chunks: list[list[int]] = []
            ranges: list[tuple[int, int]] = []
            for text in texts:
                tokenized = model.tokenizer(
                    text,
                    add_special_tokens=True,
                    truncation=True,
                    max_length=min(
                        self.settings.window_tokens, int(model.max_seq_length)
                    ),
                    stride=self.settings.window_overlap_tokens,
                    return_overflowing_tokens=True,
                    return_attention_mask=True,
                    verbose=False,
                )
                windows = tokenized["input_ids"]
                if not windows:
                    raise RetrievalError("Text has no model token windows.")
                start = len(chunks)
                chunks.extend(windows)
                ranges.append((start, len(chunks)))
            batches: list[Vector] = []
            with torch.inference_mode():
                for start in range(0, len(chunks), self.settings.batch_size):
                    features = model.tokenizer.pad(
                        {"input_ids": chunks[start : start + self.settings.batch_size]},
                        padding=True,
                        return_tensors="pt",
                    )
                    features = {
                        key: value.to(model.device) for key, value in features.items()
                    }
                    batches.append(
                        np.asarray(
                            model(features)["sentence_embedding"]
                            .detach()
                            .cpu()
                            .numpy(),
                            dtype=np.float32,
                        )
                    )
            encoded = unit(np.concatenate(batches, axis=0))
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

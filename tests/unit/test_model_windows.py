"""Verify overflow token coverage without importing or loading an ML model."""

import sys
from contextlib import nullcontext
from types import ModuleType
from typing import Self

import numpy as np
import pytest

from lextrace.retrieval.models import SentenceEncoder, Vector
from lextrace.retrieval.settings import DenseSettings


class Tensor:
    def __init__(self, value: Vector) -> None:
        self.value = value

    def to(self, device: str) -> Self:
        return self

    def detach(self) -> Self:
        return self

    def cpu(self) -> Self:
        return self

    def numpy(self) -> Vector:
        return self.value


class Tokenizer:
    def __init__(self) -> None:
        self.seen: list[int] = []

    def __call__(self, text: str, **kwargs: object) -> dict[str, list[list[int]]]:
        assert kwargs["return_overflowing_tokens"] is True
        assert kwargs["max_length"] == 16
        assert kwargs["stride"] == 2
        tokens = list(range(1, int(text) + 1))
        width = 14
        step = width - 2
        return {
            "input_ids": [
                [1000, *tokens[i : i + width], 1001]
                for i in range(0, len(tokens), step)
            ]
        }

    def pad(
        self, features: dict[str, list[list[int]]], **kwargs: object
    ) -> dict[str, Tensor]:
        rows = features["input_ids"]
        assert len(rows) <= 2
        self.seen.extend(token for row in rows for token in row if token < 1000)
        width = max(len(row) for row in rows)
        return {
            "input_ids": Tensor(
                np.asarray(
                    [row + [0] * (width - len(row)) for row in rows], dtype=np.float32
                )
            )
        }


class Model:
    max_seq_length = 16
    device = "cpu"

    def __init__(self) -> None:
        self.tokenizer = Tokenizer()

    def __call__(self, features: dict[str, Tensor]) -> dict[str, Tensor]:
        sums = features["input_ids"].value.sum(axis=1)
        return {
            "sentence_embedding": Tensor(
                np.asarray([[float(s), 1.0] for s in sums], dtype=np.float32)
            )
        }


def test_token_windows_preserve_boundaries_and_are_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TorchStub(ModuleType):
        inference_mode = staticmethod(nullcontext)

    stub = TorchStub("torch")
    monkeypatch.setitem(sys.modules, "torch", stub)
    model = Model()
    encoder = SentenceEncoder(
        DenseSettings(batch_size=2, window_tokens=16, window_overlap_tokens=2)
    )
    encoder._model = model
    vectors = encoder.encode(["31", "5"])
    seen = model.tokenizer.seen
    assert set(seen) == set(range(1, 32))
    assert all(token in seen for token in (4, 5, 8, 9, 28, 29, 30, 31))
    assert vectors.shape == (2, 2)
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1)
    model.tokenizer.seen.clear()
    assert np.array_equal(vectors, encoder.encode(["31", "5"]))


def test_one_and_multiple_window_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    class TorchStub(ModuleType):
        inference_mode = staticmethod(nullcontext)

    monkeypatch.setitem(sys.modules, "torch", TorchStub("torch"))
    model = Model()
    encoder = SentenceEncoder(
        DenseSettings(batch_size=2, window_tokens=16, window_overlap_tokens=2)
    )
    encoder._model = model
    vectors = encoder.encode(["3", "20"])
    assert vectors.shape == (2, 2)
    assert model.tokenizer.seen[:3] == [1, 2, 3]
    assert set(model.tokenizer.seen[3:]) == set(range(1, 21))

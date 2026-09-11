"""Atomic portable corpus snapshot and memory-mapped dense index. No pickle."""

import hashlib
import json
import tempfile
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import Field, ValidationError

from lextrace.corpus import serialize_cases
from lextrace.evaluation.benchmark import digest
from lextrace.retrieval.contracts import Record, RetrievalError
from lextrace.retrieval.documents import Corpus
from lextrace.retrieval.models import Encoder, SentenceEncoder, Vector, unit
from lextrace.retrieval.passages import segment
from lextrace.retrieval.settings import EngineConfig


def file_hash(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def representation(config: EngineConfig) -> str:
    return digest(
        json.dumps(
            {
                "model": config.dense.model,
                "revision": config.dense.revision,
                "passages": config.passages.words,
                "preprocessing": config.preprocessing,
                "pooling": "unit-mean-all-token-windows-and-passages-v1",
            },
            sort_keys=True,
        )
    )


class IndexMetadata(Record):
    format_version: Literal[1] = 1
    corpus_hash: str
    document_count: int
    source_corpus: str
    representation_hash: str
    config: EngineConfig
    files: dict[str, str]
    dense: bool
    dimension: int = 0
    encoding_device: str | None = None
    backend: Literal["numpy-mmap-exact"] = "numpy-mmap-exact"
    bm25_storage: Literal["reconstructed-from-canonical-snapshot"] = (
        "reconstructed-from-canonical-snapshot"
    )
    packages: dict[str, str] = Field(default_factory=dict)


class LocalIndex:
    def __init__(
        self,
        path: Path,
        *,
        corpus_path: Path | None = None,
        config: EngineConfig | None = None,
    ) -> None:
        try:
            self.metadata = IndexMetadata.model_validate_json(
                (path / "metadata.json").read_bytes()
            )
            expected = {"corpus.jsonl", "document_map.json"} | (
                {"dense.npy"} if self.metadata.dense else set()
            )
            if set(self.metadata.files) != expected or any(
                file_hash(path / name) != checksum
                for name, checksum in self.metadata.files.items()
            ):
                raise RetrievalError(
                    "Index file checksums do not match; rebuild into a new directory."
                )
            self.corpus = Corpus.load(path / "corpus.jsonl")
            ids = json.loads((path / "document_map.json").read_text())
            if (
                ids != list(self.corpus.ids)
                or self.corpus.hash != self.metadata.corpus_hash
                or len(ids) != self.metadata.document_count
            ):
                raise RetrievalError("Index corpus/document alignment is invalid.")
            self.config = config or self.metadata.config
            if representation(self.config) != self.metadata.representation_hash:
                raise RetrievalError(
                    "Index representation/model configuration mismatch."
                )
            if (
                corpus_path is not None
                and Corpus.load(corpus_path).hash != self.corpus.hash
            ):
                raise RetrievalError("Supplied corpus differs from the indexed corpus.")
            self.vectors: Vector | None = None
            if self.metadata.dense:
                vectors = np.load(path / "dense.npy", mmap_mode="r", allow_pickle=False)
                if (
                    vectors.dtype != np.float32
                    or vectors.shape != (len(ids), self.metadata.dimension)
                    or self.metadata.dimension < 1
                ):
                    raise RetrievalError("Invalid dense matrix shape or type.")
                for start in range(0, len(ids), self.config.dense.block_size):
                    block = vectors[start : start + self.config.dense.block_size]
                    if not np.isfinite(block).all() or not np.allclose(
                        np.linalg.norm(block, axis=1), 1, atol=1e-4
                    ):
                        raise RetrievalError("Invalid dense vector normalization.")
                self.vectors = vectors
        except RetrievalError:
            raise
        except (OSError, ValueError, ValidationError):
            raise RetrievalError("Could not load a valid local index.") from None


def build_index(
    corpus_path: Path,
    output: Path,
    config: EngineConfig | None = None,
    *,
    encoder: Encoder | None = None,
    dense: bool = True,
) -> IndexMetadata:
    config = config or EngineConfig()
    corpus = Corpus.load(corpus_path)
    if not output.resolve().is_relative_to(Path("artifacts/indexes").resolve()):
        raise RetrievalError("Indexes must be stored under artifacts/indexes/.")
    if output.exists():
        existing = LocalIndex(output, corpus_path=corpus_path, config=config)
        if existing.metadata.dense != dense:
            raise RetrievalError(
                "Index capabilities differ; use a new output directory."
            )
        return existing.metadata
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            dir=output.parent, prefix=".index-"
        ) as temporary:
            destination = Path(temporary) / "index"
            destination.mkdir()
            (destination / "corpus.jsonl").write_text(
                serialize_cases(corpus.cases), encoding="utf-8"
            )
            (destination / "document_map.json").write_text(
                json.dumps(list(corpus.ids), separators=(",", ":")) + "\n"
            )
            dimension = 0
            files = ["corpus.jsonl", "document_map.json"]
            if dense:
                model = encoder or SentenceEncoder(config.dense)
                document_vectors = []
                for case in corpus.cases:
                    passages = segment(case, config.passages)
                    total: Vector | None = None
                    count = 0
                    for start in range(0, len(passages), config.dense.batch_size):
                        batch = passages[start : start + config.dense.batch_size]
                        encoded = unit(
                            np.asarray(
                                model.encode([p.text for p in batch]), dtype=np.float32
                            )
                        )
                        if len(encoded) != len(batch):
                            raise RetrievalError("Encoder row count mismatch.")
                        subtotal = encoded.sum(axis=0)
                        total = subtotal if total is None else total + subtotal
                        count += len(encoded)
                    assert total is not None
                    document_vectors.append(total / count)
                matrix = unit(np.asarray(document_vectors, dtype=np.float32))
                dimension = matrix.shape[1]
                np.save(destination / "dense.npy", matrix, allow_pickle=False)
                files.append("dense.npy")
            packages = {}
            for name in ("numpy", "sentence-transformers", "torch", "transformers"):
                try:
                    packages[name] = version(name)
                except PackageNotFoundError:
                    pass
            metadata = IndexMetadata(
                corpus_hash=corpus.hash,
                document_count=len(corpus.ids),
                source_corpus=str(corpus_path.resolve()),
                representation_hash=representation(config),
                config=config,
                files={name: file_hash(destination / name) for name in files},
                dense=dense,
                dimension=dimension,
                encoding_device=str(getattr(model, "device", "injected"))
                if dense
                else None,
                packages=packages,
            )
            (destination / "metadata.json").write_text(
                metadata.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )
            destination.rename(output)
        return metadata
    except RetrievalError:
        raise
    except Exception:
        raise RetrievalError(
            "Index construction failed; no partial index was published."
        ) from None

"""Offline versioned corpus publishing and explicit index rebuild smoke."""

import json
from pathlib import Path

import pytest

from lextrace.cli import main
from lextrace.corpus import read_cases, serialize_cases
from tests.unit.test_monitoring import case


def test_update_corpus_publishes_versioned_snapshot_and_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    base = data / "base.jsonl"
    batch = data / "batch.jsonl"
    output = data / "versions" / "next.jsonl"
    index = tmp_path / "artifacts" / "indexes" / "next"
    base.write_text(
        serialize_cases([case("1", "Original source text.")]), encoding="utf-8"
    )
    batch.write_text(
        serialize_cases(
            [case("1", "Updated source text."), case("2", "New source text.")]
        ),
        encoding="utf-8",
    )
    command = [
        "update-corpus",
        "--base",
        str(base),
        "--batch",
        str(batch),
        "--output",
        str(output),
        "--index-output",
        str(index),
        "--bm25-only",
    ]
    main(command)
    report = json.loads(capsys.readouterr().out)
    assert report["added_case_ids"] == ["2"]
    assert report["text_changed_case_ids"] == ["1"]
    assert report["index_strategy"] == "full_atomic_rebuild"
    assert [item.source_id for item in read_cases(output)] == ["1", "2"]
    assert (index / "metadata.json").exists()
    main(command)
    assert json.loads(capsys.readouterr().out)["new_version"] == report["new_version"]

"""Offline versioned corpus publishing and explicit index rebuild smoke."""

import json
from pathlib import Path

import httpx
import pytest

from lextrace.cli import main
from lextrace.corpus import read_cases, serialize_cases
from lextrace.matter.store import MatterStore
from tests.integration.test_ingest_corpus import cluster, details
from tests.unit.test_monitoring import case, matter_store


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


def test_refresh_monitoring_ingests_rebuilds_and_runs_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COURTLISTENER_API_TOKEN", "synthetic-credential")
    monkeypatch.setenv("LEXTRACE_MATTER_DB", str(tmp_path / "matter.sqlite3"))
    monkeypatch.setenv("LEXTRACE_PRIVATE_MATTER_ROOT", str(tmp_path / "private"))
    store, matter_id, _ = matter_store(tmp_path)
    store.close()
    data = tmp_path / "data"
    data.mkdir()
    base = data / "base.jsonl"
    base.write_text(serialize_cases([case("1", "Old case text.")]), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Token synthetic-credential"
        if request.url.path.endswith("clusters/"):
            return httpx.Response(200, json={"results": [cluster(2)], "next": None})
        if "/opinions/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "id": 2,
                    "type": "010combined",
                    "plain_text": "The employee received notice before termination.",
                },
            )
        return details(request)

    output = data / "version-2.jsonl"
    index = tmp_path / "artifacts/indexes/version-2"
    command = [
        "refresh-monitoring",
        "--base",
        str(base),
        "--court",
        "ca2",
        "--max-cases",
        "1",
        "--request-interval",
        "0.001",
        "--output",
        str(output),
        "--index-output",
        str(index),
        "--bm25-only",
        "--matter-id",
        matter_id,
    ]
    main(command, transport=httpx.MockTransport(handler))
    report = json.loads(capsys.readouterr().out)
    assert report["added_case_ids"] == ["2"]
    assert report["index_strategy"] == "full_atomic_rebuild"
    assert report["run_id"]
    assert [item.source_id for item in read_cases(output)] == ["1", "2"]
    assert (index / "metadata.json").exists()
    assert "synthetic-credential" not in json.dumps(report)
    saved = MatterStore(tmp_path / "matter.sqlite3", tmp_path / "private")
    assert saved.monitoring_run(report["run_id"]) is not None
    saved.close()

    second = data / "version-3.jsonl"
    second_index = tmp_path / "artifacts/indexes/version-3"
    main(
        [
            "refresh-monitoring",
            "--base",
            str(output),
            "--court",
            "ca2",
            "--max-cases",
            "1",
            "--request-interval",
            "0.001",
            "--output",
            str(second),
            "--index-output",
            str(second_index),
            "--old-index",
            str(index),
            "--bm25-only",
            "--matter-id",
            matter_id,
        ],
        transport=httpx.MockTransport(handler),
    )
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["added_case_ids"] == []
    assert repeated["outcome"] == "NO_MATERIAL_CHANGE"
    assert repeated["old_index_identity"] == report["new_index_identity"]


def test_refresh_monitoring_request_failure_preserves_batch_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COURTLISTENER_API_TOKEN", "synthetic-credential")
    data = tmp_path / "data"
    data.mkdir()
    base = data / "base.jsonl"
    base.write_text(serialize_cases([case("1", "Old case text.")]), encoding="utf-8")
    output = data / "failed.jsonl"
    with pytest.raises(SystemExit) as stopped:
        main(
            [
                "refresh-monitoring",
                "--base",
                str(base),
                "--court",
                "ca2",
                "--max-cases",
                "1",
                "--request-interval",
                "0.001",
                "--output",
                str(output),
                "--index-output",
                str(tmp_path / "artifacts/indexes/failed"),
                "--bm25-only",
            ],
            transport=httpx.MockTransport(
                lambda request: httpx.Response(429, headers={"Retry-After": "5"})
            ),
        )
    assert stopped.value.code == 1
    assert output.with_suffix(".batch.manifest.json").is_file()
    assert not output.exists()

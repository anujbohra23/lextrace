"""Offline monitoring targets, background runs, and alert review routes."""

import time
from pathlib import Path

from fastapi.testclient import TestClient

from lextrace.api.app import create_app
from lextrace.config import AppSettings
from lextrace.corpus import serialize_cases
from lextrace.retrieval.documents import Corpus
from lextrace.retrieval.engine import LexTraceRetriever
from tests.unit.test_monitoring import case, matter_store


def test_monitoring_api_and_background_no_change(
    tmp_path: Path, monkeypatch: object
) -> None:
    from pytest import MonkeyPatch

    assert isinstance(monkeypatch, MonkeyPatch)
    monkeypatch.chdir(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    corpus = Corpus([case("1", "Old synthetic opinion.")])
    new_path = data / "same.jsonl"
    new_path.write_text(serialize_cases(corpus.cases), encoding="utf-8")
    store, matter_id, claim_id = matter_store(tmp_path)
    store.close()
    settings = AppSettings(
        matter_db=tmp_path / "matter.sqlite3",
        private_matter_root=tmp_path / "private",
    )
    engine = LexTraceRetriever(corpus)
    with TestClient(create_app(retriever=engine, settings=settings)) as client:
        overview = client.get(f"/matters/{matter_id}/monitoring")
        assert overview.status_code == 200
        assert len(overview.json()["targets"]) == 1
        target = overview.json()["targets"][0]
        assert target["target_reference_id"] == claim_id
        paused = client.patch(
            f"/matters/{matter_id}/monitoring/targets/{target['target_id']}",
            json={"enabled": False},
        )
        assert paused.status_code == 200
        assert paused.json()["enabled"] is False
        resumed = client.patch(
            f"/matters/{matter_id}/monitoring/targets/{target['target_id']}",
            json={"enabled": True},
        )
        assert resumed.status_code == 200
        assert client.get(f"/matters/{matter_id}/alerts").json() == []
        assert (
            client.post(
                "/monitoring/run",
                json={"new_corpus_path": str(tmp_path / "outside.jsonl")},
            ).status_code
            == 400
        )
        accepted = client.post(
            "/monitoring/run",
            json={"new_corpus_path": str(new_path), "matter_ids": [matter_id]},
        )
        assert accepted.status_code == 202
        run_id = accepted.json()["run_id"]
        for _ in range(30):
            status = client.get(f"/monitoring/runs/{run_id}")
            if status.json()["status"] == "completed":
                break
            time.sleep(0.01)
        assert status.status_code == 200
        assert status.json()["outcome"] == "NO_MATERIAL_CHANGE"
        assert client.get(f"/matters/{matter_id}/monitoring").json()["recent_runs"]
        assert (
            client.delete(
                f"/matters/{matter_id}/monitoring/targets/{target['target_id']}"
            ).status_code
            == 204
        )

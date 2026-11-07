"""Explicit synthetic operator configuration and isolated API storage."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def operator_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEXTRACE_OPERATOR_ID", "test-operator")
    monkeypatch.setenv(
        "LEXTRACE_OPERATOR_API_SECRET",
        "test-only-credential-00000000000000000000000000",
    )
    monkeypatch.setenv("LEXTRACE_MATTER_DB", str(tmp_path / "operator.sqlite3"))
    monkeypatch.setenv("LEXTRACE_PRIVATE_MATTER_ROOT", str(tmp_path / "private"))
    monkeypatch.delenv("LEXTRACE_ADOPT_LEGACY_MATTERS", raising=False)

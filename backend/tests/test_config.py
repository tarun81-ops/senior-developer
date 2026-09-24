"""Tests for settings/env loading behaviour."""

from __future__ import annotations

import os
from pathlib import Path

from backend.core.config import load_env


def test_dotenv_wins_over_a_stale_shell_variable(tmp_path: Path, monkeypatch) -> None:
    """A key pasted into .env must be the one that takes effect.

    Regression: a revoked OPENROUTER_API_KEY exported in the shell used to
    shadow .env (load_env defaulted to override=False), so editing .env
    appeared to do nothing.
    """
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=from-file\n", encoding="utf-8")
    monkeypatch.setenv("OPENROUTER_API_KEY", "stale-shell-value")

    loaded = load_env(tmp_path)

    assert loaded == tmp_path / ".env"
    assert os.environ["OPENROUTER_API_KEY"] == "from-file"


def test_missing_dotenv_touches_nothing(tmp_path: Path, monkeypatch) -> None:
    """No .env file -> the shell environment is left exactly as it was."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "shell-only")

    loaded = load_env(tmp_path)

    assert loaded is None
    assert os.environ["OPENROUTER_API_KEY"] == "shell-only"

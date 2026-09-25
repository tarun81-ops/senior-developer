"""An installed app's data root holds user data only (Packaging P1; D33)."""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.core.config import PACKAGE_ROOT, Settings
from backend.core.runtime import Runtime


def test_a_root_without_config_uses_the_bundled_config(tmp_path: Path) -> None:
    settings = Settings.from_root(tmp_path)
    assert settings.config_dir == PACKAGE_ROOT / "config"
    # user data still lives under the root
    assert settings.data_dir == tmp_path.resolve() / "data"
    assert settings.workspace_dir == tmp_path.resolve() / "workspace"
    assert settings.env_path == tmp_path.resolve() / ".env"


def test_a_root_with_its_own_config_keeps_it(tmp_path: Path) -> None:
    shutil.copytree(PACKAGE_ROOT / "config", tmp_path / "config")
    assert Settings.from_root(tmp_path).config_dir == tmp_path.resolve() / "config"


def test_agents_find_their_prompts_from_an_empty_data_root(tmp_path: Path) -> None:
    runtime = Runtime.create(root=tmp_path, echo=False, persist=False)
    try:
        for name in runtime.registry.pipeline.stages:
            assert runtime.agent(name).system_prompt, name
    finally:
        runtime.close()
    assert not (tmp_path / "config").exists()  # nothing was copied in


def test_the_api_serves_settings_from_an_empty_data_root(tmp_path: Path) -> None:
    app = create_app(root=tmp_path, token="t")
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        body = client.get("/api/settings", headers={"X-API-Key": "t"}).json()
    assert {a["agent"] for a in body["agents"]} >= {"planner", "coder", "reviewer"}
    assert (tmp_path / "data" / "app.db").exists()  # state went to the data root

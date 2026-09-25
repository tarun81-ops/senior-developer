"""Settings API (Part A step 4, D22): model overrides and write-only keys.

State lives in ``tmp_path``, which holds a copy of the tracked ``config/``, so
validation runs against the real configured names without touching the repo.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.core.config import PACKAGE_ROOT, Settings
from backend.core.provider.registry import Registry

TOKEN = "test-token-not-secret"
KEY_VARS = ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY")
FAKE_KEY = "AIza-fake-test-key-0123456789"


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    shutil.copytree(PACKAGE_ROOT / "config", tmp_path / "config")
    # Key status must not depend on the developer's shell or credential store,
    # and anything load_env sets during a test is undone afterwards.
    for name in KEY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(sys.modules, "keyring", None)
    return tmp_path


def make_client(root: Path) -> TestClient:
    client = TestClient(create_app(root=root, token=TOKEN), base_url="http://127.0.0.1:8765")
    client.headers.update({"X-API-Key": TOKEN})
    return client


@pytest.fixture
def client(root: Path):
    with make_client(root) as test_client:
        yield test_client


def chain(body: dict, agent: str) -> list[tuple[str, str]]:
    entry = next(a for a in body["agents"] if a["agent"] == agent)
    return [(r["provider"], r["model"]) for r in entry["routing"]]


def registry_chain(root: Path, agent: str) -> list[tuple[str, str]]:
    """What the next run would get: a fresh Registry.load, as Runtime.create does."""
    registry = Registry.load(Settings.from_root(root))
    return [(c.provider, c.model) for c in registry.agent(agent).routing]


PIN = {"provider": "groq", "model": "openai/gpt-oss-120b"}
OVERRIDES = {"agents": {"coder": PIN}, "provider_order": ["openrouter", "gemini"]}


def test_get_returns_tracked_defaults(client: TestClient, root: Path) -> None:
    body = client.get("/api/settings").json()
    assert chain(body, "coder")[0] == ("gemini", "gemini-3.8-flash")
    assert body["provider_order"] == []
    assert all(a["override"] is None for a in body["agents"])
    assert body["keys"] == {name: "missing" for name in KEY_VARS}
    assert {p["provider"] for p in body["providers"]} >= {"gemini", "groq", "openrouter", "mock"}
    assert not (root / "data" / "settings.local.yaml").exists()


def test_put_persists_override_and_next_load_uses_it(client: TestClient, root: Path) -> None:
    tracked = (root / "config" / "agents.yaml").read_bytes()

    response = client.put("/api/settings", json=OVERRIDES)
    assert response.status_code == 200, response.text
    assert chain(response.json(), "coder")[0] == ("groq", "openai/gpt-oss-120b")

    saved = yaml.safe_load((root / "data" / "settings.local.yaml").read_text(encoding="utf-8"))
    assert saved == OVERRIDES
    assert (root / "config" / "agents.yaml").read_bytes() == tracked  # never edited

    # Pin goes first; the configured chain stays behind it, de-duplicated.
    coder = registry_chain(root, "coder")
    assert coder[0] == ("groq", "openai/gpt-oss-120b")
    assert coder.count(("groq", "openai/gpt-oss-120b")) == 1
    assert coder[-1] == ("mock", "mock-echo")
    # provider_order only swaps the slots its providers hold (gemini 0, openrouter 2).
    assert [p for p, _ in registry_chain(root, "planner")] == [
        "openrouter", "groq", "gemini", "mock"
    ]
    # Providers it does not name keep their place: the demo still fails first.
    assert registry_chain(root, "demo")[0][0] == "mock_flaky"

    # An empty PUT restores the tracked defaults.
    assert client.put("/api/settings", json={}).status_code == 200
    assert registry_chain(root, "coder")[0] == ("gemini", "gemini-3.8-flash")


def test_a_registry_already_loaded_is_not_mutated(client: TestClient, root: Path) -> None:
    """A run in progress holds the registry it started with."""
    running = Registry.load(Settings.from_root(root))
    assert client.put("/api/settings", json=OVERRIDES).status_code == 200
    assert running.agent("coder").routing[0].provider == "gemini"


@pytest.mark.parametrize(
    "body",
    [
        {"agents": {"coder": {"provider": "groq", "model": "no-such-model"}}},
        {"agents": {"coder": {"provider": "nope", "model": "mock-echo"}}},
        {"agents": {"no_such_agent": PIN}},
        {"provider_order": ["gemini", "nope"]},
        {"provider_order": ["gemini", "gemini"]},
        {"agents": {"coder": {**PIN, "temperature": 2}}},
        {"unexpected": True},
    ],
)
def test_unknown_names_are_rejected(client: TestClient, root: Path, body: dict) -> None:
    response = client.put("/api/settings", json=body)
    assert response.status_code == 422, response.text
    assert not (root / "data" / "settings.local.yaml").exists()


def test_keys_are_write_only(client: TestClient, root: Path) -> None:
    (root / ".env").write_text("# mine\nAGENTS_DEBUG=0\nGEMINI_API_KEY=\n", encoding="utf-8")

    response = client.put("/api/settings/keys", json={"keys": {"GEMINI_API_KEY": FAKE_KEY}})
    assert response.status_code == 200, response.text
    assert response.json()["keys"] == {
        "GEMINI_API_KEY": "set", "GROQ_API_KEY": "missing", "OPENROUTER_API_KEY": "missing"
    }
    assert FAKE_KEY not in response.text
    for path in ("/api/settings", "/api/health"):
        assert FAKE_KEY not in client.get(path).text

    env = (root / ".env").read_text(encoding="utf-8").splitlines()
    assert env == ["# mine", "AGENTS_DEBUG=0", f"GEMINI_API_KEY={FAKE_KEY}"]
    assert not (root / "data" / "settings.local.yaml").exists()  # keys never go there


@pytest.mark.parametrize(
    "keys",
    [
        {"NOT_A_PROVIDER_KEY": FAKE_KEY},
        {"GEMINI_API_KEY": "has space\nINJECTED=1"},
        {"GEMINI_API_KEY": "${HOME}"},
        {"GEMINI_API_KEY": ""},
    ],
)
def test_bad_keys_are_rejected_without_echo(client: TestClient, root: Path, keys: dict) -> None:
    response = client.put("/api/settings/keys", json={"keys": keys})
    assert response.status_code == 422
    for value in keys.values():
        if value:
            assert value not in response.text
    assert not (root / ".env").exists()


def test_settings_routes_need_the_token(client: TestClient) -> None:
    for method, path in (
        ("GET", "/api/settings"), ("PUT", "/api/settings"), ("PUT", "/api/settings/keys")
    ):
        response = client.request(method, path, json={}, headers={"X-API-Key": "wrong"})
        assert response.status_code == 401


def test_override_survives_a_restart(root: Path) -> None:
    with make_client(root) as first:
        assert first.put("/api/settings", json=OVERRIDES).status_code == 200
    with make_client(root) as second:
        body = second.get("/api/settings").json()
    coder = next(a for a in body["agents"] if a["agent"] == "coder")
    assert coder["override"] == PIN
    assert body["provider_order"] == ["openrouter", "gemini"]

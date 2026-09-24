"""API foundation tests: security, storage, and the app factory.

All state is rooted in ``tmp_path`` and no network or model provider is used.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.models import EventResponse
from backend.api.repository import SqliteEventRepository, StoredEvent
from backend.api.security import (
    ALLOWED_ORIGINS,
    LaunchSecurity,
    is_allowed_host,
    is_allowed_origin,
    new_api_token,
    require_token,
)
from backend.core.events import Event, EventBus, JsonlWriter


@pytest.fixture
def token() -> str:
    return "test-token-not-secret"


@pytest.fixture
def client(tmp_path: Path, token: str):
    """A TestClient with the lifespan running, rooted entirely in tmp_path.

    ``base_url`` is a loopback address because the app refuses any Host header
    that is not loopback — the test client would otherwise send ``testserver``.
    """
    app = create_app(root=tmp_path, token=token)
    with TestClient(app, base_url="http://127.0.0.1:8765") as test_client:
        test_client.headers.update({"X-API-Key": token})
        yield test_client


# -- security ---------------------------------------------------------------
def test_token_is_random_per_launch() -> None:
    assert new_api_token() != new_api_token()
    assert len(new_api_token()) >= 32


def test_only_loopback_hosts_are_accepted() -> None:
    assert is_allowed_host("127.0.0.1:8765")
    assert is_allowed_host("localhost")
    assert is_allowed_host("[::1]:8765")
    assert not is_allowed_host("evil.example.com")
    assert not is_allowed_host("192.168.1.5:8765")
    assert not is_allowed_host("127.0.0.1:not-a-port")
    assert not is_allowed_host("")


def test_only_local_ui_origins_are_allowed() -> None:
    assert is_allowed_origin(None)  # Electron main process / curl
    for origin in ALLOWED_ORIGINS:
        assert is_allowed_origin(origin)
    assert not is_allowed_origin("https://evil.example.com")


def test_every_api_request_requires_the_token(client: TestClient, token: str) -> None:
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/health", headers={"X-API-Key": "wrong"}).status_code == 401
    del client.headers["X-API-Key"]
    assert client.get("/api/health").status_code == 401
    client.headers.update({"X-API-Key": token})


def test_host_header_must_be_loopback(client: TestClient) -> None:
    response = client.get("/api/health", headers={"Host": "evil.example.com"})
    assert response.status_code == 400


def test_foreign_origin_is_rejected(client: TestClient) -> None:
    response = client.get("/api/health", headers={"Origin": "https://evil.example.com"})
    assert response.status_code == 403


def test_oversized_body_is_refused(client: TestClient) -> None:
    """The cap is enforced by the router dependency before any handler runs.

    Step 1 has no POST route yet, so the dependency is exercised through a
    throwaway app that mounts it the same way the real router does; the first
    real POST endpoint (step 2) inherits the same check.
    """
    from fastapi import Depends, FastAPI

    probe = FastAPI()
    probe.state.security = LaunchSecurity(token="t")
    probe_api = APIRouter(prefix="/api", dependencies=[Depends(require_token)])

    @probe_api.post("/probe")
    async def probe_endpoint() -> dict[str, bool]:
        return {"ok": True}

    probe.include_router(probe_api)

    with TestClient(probe, base_url="http://127.0.0.1:8765") as probe_client:
        headers = {"X-API-Key": "t"}
        assert probe_client.post("/api/probe", json={"x": 1}, headers=headers).status_code == 200
        too_big = probe_client.post(
            "/api/probe", content=b"x" * 300_000, headers=headers
        )
        assert too_big.status_code == 413
        assert b"limit" in too_big.content


def test_content_length_must_be_a_number(client: TestClient) -> None:
    bad = client.get("/api/health", headers={"Content-Length": "not-a-number"})
    assert bad.status_code == 400


def test_cors_preflight_allows_the_vite_origin_only(client: TestClient) -> None:
    ok = client.options(
        "/api/health",
        headers={
            "Origin": "http://127.0.0.1:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "X-API-Key",
        },
    )
    assert ok.status_code == 200
    assert ok.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
    denied = client.options(
        "/api/health",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert "access-control-allow-origin" not in denied.headers


def test_health_reports_the_bound_port_and_auth(client: TestClient) -> None:
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["api_host"] == "127.0.0.1"
    assert body["auth"] == "ok"
    # the port from base_url, so a launcher can point the shell at the server
    assert body["api_port"] == 8765


# -- storage ----------------------------------------------------------------
def _event(run_id: str, seq: int) -> Event:
    return Event(
        kind="llm.response" if seq % 2 else "agent.start",
        message=f"message {seq}",
        run_id=run_id,
        agent="coder",
        data={"seq": seq},
    )


def test_repository_round_trips_and_replays_after_a_cursor(tmp_path: Path) -> None:
    repo = SqliteEventRepository(tmp_path / "app.db")
    for seq in range(3):
        repo.append(_event("run-a", seq), seq=seq)

    assert repo.last_seq("run-a") == 2
    assert [e.seq for e in repo.get_events("run-a")] == [0, 1, 2]
    assert [e.seq for e in repo.get_events("run-a", after_seq=0)] == [1, 2]
    assert repo.get_events("run-a", after_seq=2) == []
    assert repo.get_events("missing-run") == []


def test_repository_ignores_duplicate_sequence_numbers(tmp_path: Path) -> None:
    repo = SqliteEventRepository(tmp_path / "app.db")
    repo.append(_event("run-a", 0), seq=0)
    repo.append(_event("run-a", 0), seq=0)  # replay is idempotent
    assert len(repo.get_events("run-a")) == 1


def test_repository_lists_run_ids_most_recent_first(tmp_path: Path) -> None:
    repo = SqliteEventRepository(tmp_path / "app.db")
    repo.append(_event("run-a", 0), seq=0)
    repo.append(_event("run-b", 0), seq=0)
    assert repo.run_ids() == ["run-b", "run-a"]


def test_stored_event_round_trips_all_fields() -> None:
    stored = StoredEvent.from_event(_event("r", 7), seq=7)
    assert EventResponse(**stored.to_dict()).data == {"seq": 7}


# -- event replay endpoint --------------------------------------------------
def test_events_endpoint_replays_a_run(client: TestClient, tmp_path: Path) -> None:
    repo = SqliteEventRepository(tmp_path / "data" / "app.db")
    for seq in range(3):
        repo.append(_event("run-x", seq), seq=seq)

    body = client.get("/api/events", params={"run_id": "run-x", "after_seq": 0}).json()
    assert [event["seq"] for event in body["events"]] == [1, 2]
    assert body["last_seq"] == 2
    assert body["has_more"] is False

    first = client.get("/api/events", params={"run_id": "run-x", "limit": 1}).json()
    assert first["has_more"] is True
    assert client.get("/api/events", params={"run_id": "../escape"}).status_code == 400


# -- event bus -> database bridge -----------------------------------------
def test_event_store_numbers_persists_and_replays_bus_events(tmp_path: Path) -> None:
    from backend.api.event_store import EventStore

    store = EventStore(SqliteEventRepository(tmp_path / "app.db"))
    bus = EventBus(run_id="run-bus", jsonl=JsonlWriter(tmp_path / "e.jsonl"), echo=False)
    store.subscribe(bus)

    bus.emit("agent.start", "starting", agent="planner")
    bus.emit("llm.response", "done", agent="planner", model="m")

    replayed = store.replay("run-bus")
    assert [e.seq for e in replayed] == [0, 1]
    assert [e.kind for e in replayed] == ["agent.start", "llm.response"]
    assert replayed[0].message == "starting"
    assert store.last_seq("run-bus") == 1
    assert [e.seq for e in store.replay("run-bus", after_seq=0)] == [1]


def test_two_buses_keep_independent_sequences(tmp_path: Path) -> None:
    from backend.api.event_store import EventStore

    store = EventStore(SqliteEventRepository(tmp_path / "app.db"))
    first = EventBus(run_id="r1", jsonl=None, echo=False)
    second = EventBus(run_id="r2", jsonl=None, echo=False)
    store.subscribe(first)
    store.subscribe(second)

    first.emit("a", "one")
    first.emit("b", "two")
    second.emit("c", "three")

    assert [e.seq for e in store.replay("r1")] == [0, 1]
    assert [e.seq for e in store.replay("r2")] == [0]


def test_wait_for_returns_as_soon_as_an_event_arrives(tmp_path: Path) -> None:
    import threading

    from backend.api.event_store import EventStore

    store = EventStore(SqliteEventRepository(tmp_path / "app.db"))
    bus = EventBus(run_id="run-wait", jsonl=None, echo=False)
    store.subscribe(bus)
    bus.emit("agent.start", "already there")

    assert store.wait_for("run-wait", after_seq=-1, timeout=0.01) is None
    # after the cursor, no event exists: wait_for gives up after the timeout.
    store.wait_for("run-wait", after_seq=5, timeout=0.01)


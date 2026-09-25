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


def test_origin_null_is_rejected_and_the_custom_scheme_is_allowed() -> None:
    """Electron renderers over file:// report ``Origin: null``.

    ``null`` is not a value an allowlist can pin down (every sandboxed iframe
    and every ``data:`` page reports it too), so it stays denied; Part B serves
    the built UI from the custom ``sda://app/`` origin instead, which is a
    real, checkable origin.
    """
    assert not is_allowed_origin("null")
    assert is_allowed_origin("sda://app")
    # exact match only: no other host, port, scheme or path under sda:
    assert not is_allowed_origin("sda://app.local:5173")
    assert not is_allowed_origin("sda://evil")
    assert not is_allowed_origin("sda-evil://app")
    assert not is_allowed_origin("sda://app/../../etc")


def test_null_origin_is_refused_over_http(client: TestClient) -> None:
    response = client.get("/api/health", headers={"Origin": "null"})
    assert response.status_code == 403
    assert "null" in response.json()["detail"]


def test_custom_scheme_origin_is_accepted_over_http(client: TestClient) -> None:
    response = client.get("/api/health", headers={"Origin": "sda://app"})
    assert response.status_code == 200


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


# -- body cap on bytes actually received --------------------------------------
def _raw_post(app, chunks: list[bytes], *, headers: dict[str, str]) -> tuple[int, bytes]:
    """POST pre-built ASGI messages (no Content-Length) straight into the app.

    ``TestClient``/httpx always computes a Content-Length for a bytes body, so
    the chunked case — the one a header check cannot see — needs a hand-rolled
    ASGI call. The scope and messages are the minimum ASGI requires.
    """
    import asyncio

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "method": "POST",
        "scheme": "http",
        "path": "/api/probe",
        "raw_path": b"/api/probe",
        "query_string": b"",
        "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("127.0.0.1", 5000),
        "server": ("127.0.0.1", 8765),
    }
    sent: list[bytes] = []
    messages = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks) - 1,
        }
        for index, chunk in enumerate(chunks)
    ]

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            sent.append(str(message["status"]).encode())
        elif message["type"] == "http.response.body":
            sent.append(message.get("body", b""))

    asyncio.run(app(scope, receive, send))
    return int(sent[0]), b"".join(sent[1:])


def test_chunked_body_over_the_cap_is_refused_without_a_content_length(tmp_path: Path) -> None:
    """A chunked upload has no Content-Length, so only the byte counter can stop it."""
    from backend.api.app import create_app

    app = create_app(root=tmp_path, token="t")
    # start the lifespan so the app state exists, as a real server would
    with TestClient(app, base_url="http://127.0.0.1:8765"):
        status, body = _raw_post(
            app,
            [b"x" * 100_000 for _ in range(3)],
            headers={
                "host": "127.0.0.1:8765",
                "x-api-key": "t",
                "content-type": "text/plain",
            },
        )
    assert status == 413
    assert b"limit" in body


def test_chunked_body_under_the_cap_is_delivered_whole() -> None:
    """The cap must not truncate a legitimate body: the endpoint sees all of it."""
    from backend.api.middleware import BodySizeLimitMiddleware

    seen: dict[str, int] = {}

    async def echo_body(scope, receive, send) -> None:  # noqa: ANN001 - raw ASGI
        body = b""
        more = True
        while more:
            message = await receive()
            body += message.get("body", b"")
            more = message.get("more_body", False)
        seen["len"] = len(body)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    status, body = _raw_post(
        BodySizeLimitMiddleware(echo_body),
        [b"y" * 10_000 for _ in range(3)],
        headers={"host": "127.0.0.1:8765", "content-type": "text/plain"},
    )
    assert seen["len"] == 30_000
    assert (status, body) == (200, b"ok")


def test_oversized_body_with_a_lying_content_length_is_refused(tmp_path: Path) -> None:
    """A declared length under the cap must not smuggle a bigger body through."""
    from backend.api.app import create_app

    app = create_app(root=tmp_path, token="t")
    with TestClient(app, base_url="http://127.0.0.1:8765"):
        status, _ = _raw_post(
            app,
            [b"z" * 300_000],
            headers={
                "host": "127.0.0.1:8765",
                "x-api-key": "t",
                "content-type": "text/plain",
                "content-length": "10",  # a lie; the counter does not believe it
            },
        )
    assert status == 413


@pytest.mark.parametrize("origin", ["http://127.0.0.1:5173", "sda://app"])
def test_cors_preflight_allows_the_ui_origins_only(client: TestClient, origin: str) -> None:
    # X-API-Key is not a CORS-safelisted header, so every UI request is
    # preflighted: an origin missing here is blocked even if the Origin check
    # would accept it.
    ok = client.options(
        "/api/health",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "X-API-Key",
        },
    )
    assert ok.status_code == 200
    assert ok.headers["access-control-allow-origin"] == origin
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

    from backend.api.event_store import EventStore

    store = EventStore(SqliteEventRepository(tmp_path / "app.db"))
    bus = EventBus(run_id="run-wait", jsonl=None, echo=False)
    store.subscribe(bus)
    bus.emit("agent.start", "already there")

    assert store.wait_for("run-wait", after_seq=-1, timeout=0.01) is None
    # after the cursor, no event exists: wait_for gives up after the timeout.
    store.wait_for("run-wait", after_seq=5, timeout=0.01)


"""SSE event stream (Part A step 5, D19/D23): replay, handoff, resume, close.

Pipelines run on the offline mock providers from ``test_api_runs``; nothing
touches the network or spends quota. The approval gate is the lever that holds
a run open mid-stream, so "live" is deterministic rather than timed.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.stream import CLOSING_KINDS, event_frames
from backend.tests.test_api_runs import (
    TOKEN,
    _create,
    _wait_status,
    _wait_terminal,
    mock_factory,
)

GATED = {"stages": ["planner", "architect"], "approval_gates": ["plan"]}


@pytest.fixture
def app(tmp_path: Path):
    app = create_app(root=tmp_path, token=TOKEN, runtime_factory=mock_factory(tmp_path))
    yield app
    app.state.run_manager.shutdown()


@pytest.fixture
def client(app: FastAPI):
    test_client = TestClient(app, base_url="http://127.0.0.1:8765")
    test_client.headers.update({"X-API-Key": TOKEN})
    yield test_client
    test_client.close()


def parse(text: str) -> list[dict]:
    """Decode SSE frames the way the UI's fetch reader does; ids must match seqs."""
    events = []
    for block in text.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "data" in fields:
            event = json.loads(fields["data"])
            assert int(fields["id"]) == event["seq"]
            events.append(event)
    return events


def stream(client: TestClient, run_id: str, **params) -> list[dict]:
    """GET the stream; returns only once the server closed it."""
    headers = params.pop("headers", {})
    params = {"run_id": run_id, **params}
    response = client.get("/api/events/stream", params=params, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    return parse(response.text)


def assert_complete(events: list[dict], *, start: int = 0) -> None:
    """Contiguous seqs (no gap, no repeat) ending on the closing event."""
    assert [e["seq"] for e in events] == list(range(start, start + len(events)))
    assert events[-1]["kind"] in CLOSING_KINDS
    assert not any(e["kind"] in CLOSING_KINDS for e in events[:-1])


def collect(app: FastAPI, run_id: str, *, after_seq: int = -1, stop_after: int | None = None,
            during=None) -> list[dict]:
    """Drive the stream generator directly, so a test controls the live moment.

    ``during`` runs (on a timer thread) once the reader has caught up with
    everything persisted so far, while it is blocked waiting for live events.
    """
    store, manager = app.state.event_store, app.state.run_manager

    async def run() -> list[dict]:
        frames = event_frames(store, manager, run_id, after_seq=after_seq)
        events: list[dict] = []
        pending = during
        try:
            async for frame in frames:
                if frame.startswith(":"):
                    continue
                events.extend(parse(frame))
                if stop_after is not None and len(events) == stop_after:
                    break
                if pending is not None and events[-1]["seq"] == store.last_seq(run_id):
                    events[-1]["_caught_up"] = True  # the replay/live boundary
                    threading.Timer(0.2, pending).start()
                    pending = None
        finally:
            await frames.aclose()
        return events

    return asyncio.run(run())


# -- access ------------------------------------------------------------------
def test_stream_requires_the_token(client: TestClient) -> None:
    run_id = _create(client)["run_id"]
    response = client.get(
        "/api/events/stream", params={"run_id": run_id}, headers={"X-API-Key": "wrong"}
    )
    assert response.status_code == 401


def test_stream_of_an_unknown_run_is_404(client: TestClient) -> None:
    assert client.get("/api/events/stream", params={"run_id": "nope"}).status_code == 404


# -- replay ------------------------------------------------------------------
def test_replay_from_a_cursor_sends_only_later_events(client: TestClient) -> None:
    run_id = _create(client)["run_id"]
    _wait_terminal(client, run_id)

    full = stream(client, run_id)
    assert_complete(full)
    assert full[0]["kind"] == "api.run_queued"
    assert full[-1]["kind"] == "api.run_succeeded"

    cursor = full[4]["seq"]
    assert stream(client, run_id, after_seq=cursor) == full[5:]
    # Last-Event-ID is the standard SSE equivalent of after_seq
    assert stream(client, run_id, headers={"Last-Event-ID": str(cursor)}) == full[5:]
    # a client that already saw the closing event gets a clean, empty close
    assert stream(client, run_id, after_seq=full[-1]["seq"]) == []


# -- live ----------------------------------------------------------------------
def test_replay_then_live_handoff_drops_and_repeats_nothing(
    app: FastAPI, client: TestClient
) -> None:
    run_id = _create(client, **GATED)["run_id"]
    _wait_status(client, run_id, "waiting_approval")
    manager = app.state.run_manager

    events = collect(app, run_id, during=lambda: manager.approve(run_id))

    handoff = next(i for i, e in enumerate(events) if e.pop("_caught_up", False))
    assert events[handoff]["kind"] == "api.run_waiting_approval"  # end of replay
    assert events[handoff + 1]["kind"] == "api.run_approved"  # first live event
    assert_complete(events)
    assert events[-1]["kind"] == "api.run_succeeded"
    assert events == stream(client, run_id)  # identical to a replay after the fact


def test_reconnect_with_the_last_seq_resumes_without_gap_or_repeat(
    app: FastAPI, client: TestClient
) -> None:
    run_id = _create(client, **GATED)["run_id"]
    _wait_status(client, run_id, "waiting_approval")
    manager = app.state.run_manager

    first = collect(app, run_id, stop_after=3)  # the connection drops mid-run
    rest = collect(app, run_id, after_seq=first[-1]["seq"], during=lambda: manager.approve(run_id))
    for event in rest:
        event.pop("_caught_up", None)

    assert_complete(first + rest)
    assert first + rest == stream(client, run_id)


@pytest.mark.parametrize(
    ("action", "closing"),
    [
        ("approve", "api.run_succeeded"),
        ("reject", "api.run_failed"),
        ("cancel", "api.run_cancelled"),
    ],
)
def test_an_open_stream_closes_when_the_run_ends(
    client: TestClient, action: str, closing: str
) -> None:
    run_id = _create(client, **GATED)["run_id"]
    _wait_status(client, run_id, "waiting_approval")

    result: dict[str, list[dict]] = {}
    reader = threading.Thread(target=lambda: result.update(events=stream(client, run_id)))
    reader.start()
    reader.join(0.5)
    assert reader.is_alive(), "the stream must stay open while the run is waiting"

    assert client.post(f"/api/runs/{run_id}/{action}").status_code == 200
    reader.join(10)
    assert not reader.is_alive(), "the stream must close when the run ends"
    assert_complete(result["events"])
    assert result["events"][-1]["kind"] == closing


def test_a_run_cancelled_in_the_queue_still_gets_a_closing_event(
    app: FastAPI, client: TestClient
) -> None:
    blocker = _create(client, **GATED)["run_id"]
    _wait_status(client, blocker, "waiting_approval")
    queued = _create(client)["run_id"]
    assert client.post(f"/api/runs/{queued}/cancel").status_code == 200

    events = stream(client, queued)
    assert [e["kind"] for e in events] == [
        "api.run_queued",
        "api.run_cancel_requested",
        "api.run_cancelled",
    ]
    client.post(f"/api/runs/{blocker}/cancel")

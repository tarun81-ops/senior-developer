"""Run history across restarts (Part B, B6; D32). Offline, mock providers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.event_store import EventStore
from backend.api.models import RunOptions
from backend.api.repository import SqliteEventRepository
from backend.api.run_manager import RunRecord
from backend.core.config import get_settings
from backend.core.events import Event
from backend.tests.test_api_runs import TOKEN, _create, _wait_status, _wait_terminal, mock_factory

TESTER_REPLY = '{"files": [{"path": "src/calc.py", "content": "x = 1\\n"}]}'


def launch(root: Path) -> TestClient:
    """One app launch on ``root``; entering it runs the lifespan (and its shutdown)."""
    app = create_app(
        root=root, token=TOKEN,
        runtime_factory=mock_factory(root, replies={"tester": TESTER_REPLY}),
    )
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    client.headers.update({"X-API-Key": TOKEN})
    return client


def stream_kinds(client: TestClient, run_id: str) -> list[str]:
    body = client.get("/api/events/stream", params={"run_id": run_id}).text
    return [json.loads(line[6:])["kind"] for line in body.splitlines() if line.startswith("data: ")]


def test_runs_survive_a_restart_with_their_details(tmp_path: Path) -> None:
    with launch(tmp_path) as first:
        done = _create(first, stages=["tester"], project="calc")["run_id"]
        assert _wait_terminal(first, done)["status"] == "succeeded"
        gated = _create(first, stages=["planner"], approval_gates=["plan"])["run_id"]
        _wait_status(first, gated, "waiting_approval")
        assert first.post(f"/api/runs/{gated}/cancel").status_code == 200
        _wait_terminal(first, gated)
        before = first.get(f"/api/runs/{done}").json()

    with launch(tmp_path) as second:
        listed = second.get("/api/runs").json()
        assert [r["run_id"] for r in listed["runs"]] == [gated, done]  # newest first
        assert [r["status"] for r in listed["runs"]] == ["cancelled", "succeeded"]
        assert listed["active_run_id"] is None

        after = second.get(f"/api/runs/{done}").json()
        for key in ("request", "project", "status", "created_at", "finished_at", "agents",
                    "stage_states", "files", "result", "options"):
            assert after[key] == before[key], key
        # the files and the stream still work for a run from the last launch
        files = second.get(f"/api/runs/{done}/files").json()["files"]
        assert [f["path"] for f in files] == ["src/calc.py"]
        assert stream_kinds(second, done)[-1] == "api.run_succeeded"
        assert second.get(f"/api/runs/{gated}").json()["gate"]["gate"] == "plan"
        # finished is finished: no decision can be made on it now
        assert second.post(f"/api/runs/{gated}/approve").status_code == 409

        # and a new run still works next to the history
        fresh = _create(second, stages=["planner"], no_run_tests=True)["run_id"]
        assert _wait_terminal(second, fresh)["status"] == "succeeded"
        assert second.get("/api/runs").json()["runs"][0]["run_id"] == fresh


def seed(root: Path, record: RunRecord, events: int) -> Path:
    """Write a saved run plus some events straight into app.db, as a crash leaves it."""
    db = get_settings(root).db_path
    repo = SqliteEventRepository(db)
    for seq in range(events):
        repo.append(Event(kind="stage.start", message=f"e{seq}", run_id=record.run_id), seq=seq)
    repo.save_run(record.run_id, record.created_at, record.snapshot())
    return db


def test_a_run_left_active_by_a_crash_comes_back_interrupted(tmp_path: Path) -> None:
    crashed = RunRecord(
        run_id="20260925-120000-dead",
        request="left running",
        project="left-running",
        options=RunOptions(stages=["planner", "coder"]),
        status="running",
        created_at="2026-09-25T12:00:00+00:00",
    )
    seed(tmp_path, crashed, events=3)

    with launch(tmp_path) as client:
        state = client.get(f"/api/runs/{crashed.run_id}").json()
        assert state["status"] == "failed"
        assert state["error"] == "Interrupted: the app stopped while this run was active."
        page = client.get("/api/events", params={"run_id": crashed.run_id}).json()
        # the closing event continues the saved numbering instead of colliding
        assert [e["seq"] for e in page["events"]] == [0, 1, 2, 3]
        assert page["events"][-1]["kind"] == "api.run_failed"
        assert page["events"][-1]["data"]["reason"] == "interrupted"
        assert stream_kinds(client, crashed.run_id)[-1] == "api.run_failed"
        assert client.post(f"/api/runs/{crashed.run_id}/cancel").json()["cancelled"] is False

    # the interruption itself was saved: the next launch shows it as-is
    with launch(tmp_path) as again:
        assert again.get(f"/api/runs/{crashed.run_id}").json()["status"] == "failed"
        page = again.get("/api/events", params={"run_id": crashed.run_id}).json()
        assert len(page["events"]) == 4


def test_an_unreadable_saved_run_is_skipped_not_fatal(tmp_path: Path) -> None:
    good = RunRecord(
        run_id="20260925-120000-good", request="ok", project="ok",
        options=RunOptions(stages=["planner"]), status="succeeded",
        created_at="2026-09-25T12:00:00+00:00",
    )
    db = seed(tmp_path, good, events=0)
    SqliteEventRepository(db).save_run("broken", "2026-09-25T13:00:00+00:00", {"run_id": "broken"})
    with launch(tmp_path) as client:
        assert [r["run_id"] for r in client.get("/api/runs").json()["runs"]] == [good.run_id]


def test_event_numbering_continues_after_the_saved_events(tmp_path: Path) -> None:
    repo = SqliteEventRepository(tmp_path / "app.db")
    for seq in range(5):
        repo.append(Event(kind="k", message="m", run_id="r1"), seq=seq)
    store = EventStore(repo)  # a new process: nothing in memory
    store.sink(Event(kind="late", message="m", run_id="r1"))
    events = store.replay("r1")
    assert [e.seq for e in events] == [0, 1, 2, 3, 4, 5]
    assert events[-1].kind == "late"


@pytest.mark.parametrize("field", ["cancel_requested", "event"])
def test_live_process_state_is_never_saved(field: str) -> None:
    record = RunRecord(
        run_id="r", request="x", project="x", options=RunOptions(stages=["planner"]),
    )
    assert field not in json.dumps(record.snapshot())

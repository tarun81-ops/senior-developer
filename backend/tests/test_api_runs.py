"""API run tests: create, list, get state, cancel, and run serialisation.

Every test runs the pipeline on fake providers (``tests.helpers``), so no quota
is spent, and roots all state in ``tmp_path``.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.tests.helpers import build_pipeline_runtime

TOKEN = "test-token-not-secret"


def mock_factory(tmp_path: Path, **kwargs: Any) -> Callable[[str], Any]:
    """A runtime factory whose runtimes answer from offline mock providers.

    Each run gets its own temp root so two runs never share a board or budget.
    """

    def factory(run_id: str):
        runtime = build_pipeline_runtime(tmp_path / run_id, **kwargs)
        runtime.bus.run_id = run_id
        runtime.bus.jsonl = None
        return runtime

    return factory


@pytest.fixture
def client_factory(tmp_path: Path):
    """Build TestClients; by default their pipelines run on mock providers."""
    created: list[TestClient] = []

    def build(runtime_factory: Callable[[str], Any] | None = None) -> TestClient:
        app = create_app(
            root=tmp_path,
            token=TOKEN,
            runtime_factory=runtime_factory or mock_factory(tmp_path),
        )
        test_client = TestClient(app, base_url="http://127.0.0.1:8765")
        test_client.headers.update({"X-API-Key": TOKEN})
        created.append(test_client)
        return test_client

    yield build
    for test_client in created:
        test_client.close()


@pytest.fixture
def client(client_factory) -> TestClient:
    return client_factory()


def _wait_for(predicate: Callable[[], Any], *, timeout: float = 10.0) -> bool:
    """Poll until ``predicate()`` is truthy; False on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _wait_terminal(client: TestClient, run_id: str) -> dict:
    """Poll one run until it reaches a terminal status, then return its state."""

    def terminal() -> bool:
        return client.get(f"/api/runs/{run_id}").json()["status"] in {
            "succeeded",
            "failed",
            "cancelled",
        }

    assert _wait_for(terminal), f"run {run_id} never finished"
    return client.get(f"/api/runs/{run_id}").json()


def _create(client: TestClient, **body: Any) -> dict:
    """POST /api/runs with sensible test defaults."""
    payload: dict[str, Any] = {"request": "build a tiny calculator", "no_run_tests": True}
    payload.update(body)
    response = client.post("/api/runs", json=payload)
    assert response.status_code == 202, response.text
    return response.json()


# -- create ------------------------------------------------------------------
def test_create_run_returns_202_with_the_queued_record(client: TestClient) -> None:
    body = _create(client, project="Calc")
    assert body["status"] in {"queued", "running", "succeeded"}
    assert body["project"] == "calc"  # slugified exactly like the CLI
    assert body["request"] == "build a tiny calculator"
    assert body["options"] == {"dry_run": False, "no_apply": False, "no_run_tests": True}


def test_create_run_passes_every_option_through(client: TestClient) -> None:
    body = _create(client, project="demo", dry_run=True, no_apply=True)
    assert body["options"] == {"dry_run": True, "no_apply": True, "no_run_tests": True}


def test_create_run_rejects_an_unknown_stage(client: TestClient) -> None:
    response = client.post("/api/runs", json={"request": "x", "stages": ["nope"]})
    assert response.status_code == 422
    assert "nope" in response.text


def test_create_run_rejects_a_malformed_override(client: TestClient) -> None:
    response = client.post(
        "/api/runs", json={"request": "x", "override": [["coder", "groq"]]}
    )
    assert response.status_code == 422
    assert "agent, provider, model" in response.text


def test_create_run_rejects_an_empty_request(client: TestClient) -> None:
    response = client.post("/api/runs", json={"request": ""})
    assert response.status_code == 422


def test_create_run_rejects_unknown_fields(client: TestClient) -> None:
    """No smuggling extra keys into the queue payload."""
    response = client.post("/api/runs", json={"request": "x", "cwd": "C:/Windows"})
    assert response.status_code == 422


# -- list --------------------------------------------------------------------
def test_list_runs_is_newest_first(client: TestClient) -> None:
    first = _create(client)["run_id"]
    second = _create(client)["run_id"]
    listed = client.get("/api/runs").json()["runs"]
    assert [row["run_id"] for row in listed[:2]] == [second, first]


def test_list_runs_honours_the_limit(client: TestClient) -> None:
    for _ in range(3):
        _create(client)
    listed = client.get("/api/runs", params={"limit": 2}).json()
    assert len(listed["runs"]) == 2
    assert listed["active_run_id"] in {row["run_id"] for row in listed["runs"]} | {None}


def test_list_projects_includes_workspace_folders(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "workspace" / "older-project").mkdir(parents=True)
    (tmp_path / "workspace" / "older-project" / "main.py").write_text("x = 1", encoding="utf-8")
    _create(client, project="new-project")

    body = client.get("/api/projects").json()
    assert [row["project"] for row in body["projects"]] == ["new-project", "older-project"]
    assert body["projects"][1]["file_count"] == 1


# -- get ---------------------------------------------------------------------
def test_get_run_state_has_board_agents_and_budget(client: TestClient) -> None:
    run_id = _create(client, stages=["planner", "architect"])["run_id"]
    state = _wait_terminal(client, run_id)

    assert state["status"] == "succeeded"
    assert state["board"]["goal"] == "build a tiny calculator"
    assert [a["agent"] for a in state["agents"]] == ["planner", "architect"]
    assert all(a["output"] for a in state["agents"]), "agent output must be returned"
    assert state["budget"]["calls"] == 2
    assert state["result"]["ok"] is True
    assert state["queue_position"] is None


def test_get_run_state_lists_workspace_files(client_factory, tmp_path: Path) -> None:
    """Files come from disk, workspace-relative, never absolute paths."""
    manifest = '{"files": [{"path": "main.py", "content": "x = 1\\n"}]}'
    runtime_kwargs = {"replies": {"coder": manifest}, "apply_workspace": True}
    client = client_factory(runtime_factory=mock_factory(tmp_path, **runtime_kwargs))
    run_id = _create(client, stages=["coder"])["run_id"]
    state = _wait_terminal(client, run_id)

    assert [f["path"] for f in state["files"]] == ["main.py"]
    assert state["files"][0]["size"] == len("x = 1\n")
    assert "\\" not in state["files"][0]["path"]


def test_get_unknown_run_is_404(client: TestClient) -> None:
    assert client.get("/api/runs/does-not-exist").status_code == 404


# -- cancel ------------------------------------------------------------------
def test_cancel_a_queued_run_never_starts_it(client_factory, tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    def blocking(runtime):
        original = runtime.bus.emit

        def emit(kind, message, **data):
            if kind == "stage.start":
                entered.set()
                release.wait(5)
            return original(kind, message, **data)

        runtime.bus.emit = emit
        return runtime

    client = client_factory(runtime_factory=lambda run_id: blocking(mock_factory(tmp_path)(run_id)))
    first = _create(client, stages=["planner"], no_run_tests=True)["run_id"]
    assert entered.wait(5)
    queued = _create(client, stages=["planner"], no_run_tests=True)["run_id"]

    body = client.post(f"/api/runs/{queued}/cancel").json()
    assert body["run_id"] == queued
    assert body["cancelled"] is True
    assert body["status"] == "cancelled"

    release.set()
    assert _wait_terminal(client, queued)["status"] == "cancelled"
    assert _wait_terminal(client, first)["status"] in {"succeeded", "failed"}


def test_cancel_is_idempotent_and_reports_the_current_status(client: TestClient) -> None:
    run_id = _create(client, stages=["planner"], no_run_tests=True)["run_id"]
    assert _wait_terminal(client, run_id)["status"] in {"succeeded", "failed"}
    body = client.post(f"/api/runs/{run_id}/cancel").json()
    assert body["cancelled"] is False
    assert body["status"] in {"succeeded", "failed"}


def test_cancel_unknown_run_is_404(client: TestClient) -> None:
    assert client.post("/api/runs/nope/cancel").status_code == 404


def test_a_cancelled_running_run_stops_before_the_next_stage(
    client_factory, tmp_path: Path
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def blocking(runtime):
        original = runtime.bus.emit

        def emit(kind, message, **data):
            if kind == "stage.start" and data.get("stage") == "planner":
                entered.set()
                release.wait(5)
            return original(kind, message, **data)

        runtime.bus.emit = emit
        return runtime

    client = client_factory(runtime_factory=lambda run_id: blocking(mock_factory(tmp_path)(run_id)))
    run_id = _create(client, stages=["planner", "coder"], no_run_tests=True)["run_id"]
    assert entered.wait(5)
    client.post(f"/api/runs/{run_id}/cancel")
    release.set()

    state = _wait_terminal(client, run_id)
    assert state["status"] == "cancelled"
    # the cancel landed while planner was running, so coder never produced output
    assert "coder" not in {a["stage"] for a in state["agents"]}
    assert state["board"]["status"] == "cancelled"


# -- serialisation -----------------------------------------------------------
def test_only_one_run_executes_at_a_time(client_factory, tmp_path: Path) -> None:
    lock = threading.Lock()
    concurrent = 0
    peak = 0

    def counting(runtime):
        original = runtime.bus.emit

        def emit(kind, message, **data):
            nonlocal concurrent, peak
            if kind == "stage.end":
                with lock:
                    concurrent -= 1
            elif kind == "stage.start":
                with lock:
                    concurrent += 1
                    peak = max(peak, concurrent)
            return original(kind, message, **data)

        runtime.bus.emit = emit
        return runtime

    client = client_factory(
        runtime_factory=lambda run_id: counting(mock_factory(tmp_path)(run_id))
    )
    ids = [
        _create(client, stages=["planner", "architect"], no_run_tests=True)["run_id"]
        for _ in range(3)
    ]
    for run_id in ids:
        assert _wait_terminal(client, run_id)["status"] in {"succeeded", "failed"}
    assert peak == 1, f"runs overlapped (peak={peak})"


def test_a_cancelled_run_releases_the_lock_for_the_next_one(
    client_factory, tmp_path: Path
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def blocking(runtime):
        original = runtime.bus.emit

        def emit(kind, message, **data):
            if kind == "stage.start" and data.get("stage") == "planner":
                entered.set()
                release.wait(5)
            return original(kind, message, **data)

        runtime.bus.emit = emit
        return runtime

    client = client_factory(
        runtime_factory=lambda run_id: blocking(mock_factory(tmp_path)(run_id))
    )
    first = _create(client, stages=["planner", "architect"], no_run_tests=True)["run_id"]
    assert entered.wait(5)
    second = _create(client, stages=["planner"], no_run_tests=True)["run_id"]
    client.post(f"/api/runs/{first}/cancel")
    release.set()

    assert _wait_terminal(client, first)["status"] == "cancelled"
    assert _wait_terminal(client, second)["status"] == "succeeded"


def test_a_crashing_run_releases_the_lock_for_the_next_one(
    client_factory, tmp_path: Path
) -> None:
    """A bug outside the pipeline's own error handling must not wedge the queue."""
    raised = threading.Event()

    def exploding(runtime):
        if not raised.is_set():
            raised.set()
            raise RuntimeError("pipeline exploded")
        return runtime

    client = client_factory(
        runtime_factory=lambda run_id: exploding(mock_factory(tmp_path)(run_id))
    )
    broken = _create(client, stages=["planner"], no_run_tests=True)["run_id"]
    state = _wait_terminal(client, broken)
    assert state["status"] == "failed"
    assert "pipeline exploded" in state["error"]

    healthy = _create(client, stages=["planner"], no_run_tests=True)["run_id"]
    assert _wait_terminal(client, healthy)["status"] == "succeeded"


# -- approval gates (Part A, step 3, D21) ------------------------------------
def _wait_status(
    client: TestClient, run_id: str, status: str, *, timeout: float = 10.0
) -> dict:
    """Poll one run until it reaches ``status``, then return its state."""

    def reached() -> bool:
        return client.get(f"/api/runs/{run_id}").json()["status"] == status

    assert _wait_for(reached, timeout=timeout), f"run {run_id} never reached '{status}'"
    return client.get(f"/api/runs/{run_id}").json()


def test_create_run_rejects_an_unknown_approval_gate(client: TestClient) -> None:
    response = client.post(
        "/api/runs", json={"request": "x", "approval_gates": ["nope"]}
    )
    assert response.status_code == 422
    assert "nope" in response.text


def test_plan_gate_pauses_then_approve_resumes_the_run(client: TestClient) -> None:
    run_id = _create(
        client, stages=["planner", "architect"], approval_gates=["plan"]
    )["run_id"]

    waiting = _wait_status(client, run_id, "waiting_approval")
    gate = waiting["gate"]
    assert gate["gate"] == "plan"
    assert gate["decision"] is None
    assert gate["payload"]["stage"] == "planner"
    assert gate["payload"]["output"], "the plan being approved must be shown"
    # a waiting run keeps the single active slot: one decision at a time (D20)
    assert client.get("/api/runs").json()["active_run_id"] == run_id

    response = client.post(f"/api/runs/{run_id}/approve", json={"note": "looks good"})
    assert response.status_code == 200, response.text
    assert response.json()["decision"] == "approved"

    final = _wait_terminal(client, run_id)
    assert final["status"] == "succeeded"
    # the architect only ran because the gate was approved
    assert [a["stage"] for a in final["agents"]] == ["planner", "architect"]
    assert final["gate"]["decision"] == "approved"
    assert final["gate"]["note"] == "looks good"


def test_rejecting_the_plan_gate_fails_the_run_and_frees_the_slot(
    client: TestClient,
) -> None:
    run_id = _create(
        client, stages=["planner", "architect"], approval_gates=["plan"]
    )["run_id"]
    _wait_status(client, run_id, "waiting_approval")

    response = client.post(
        f"/api/runs/{run_id}/reject", json={"note": "wrong direction"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["decision"] == "rejected"

    final = _wait_terminal(client, run_id)
    assert final["status"] == "failed"
    assert final["result"]["reason"] == "rejected"
    # the human's own words become the run's error text
    assert "plan" in final["error"] and "wrong direction" in final["error"]
    assert final["board"]["status"] == "failed"
    assert final["gate"]["decision"] == "rejected"
    # nothing after the gate ran, and the slot is free for the next run
    assert "architect" not in {a["stage"] for a in final["agents"]}
    again = _create(client, stages=["planner"], no_run_tests=True)["run_id"]
    assert _wait_terminal(client, again)["status"] == "succeeded"


def test_cancel_wakes_a_waiting_gate_immediately(client: TestClient) -> None:
    run_id = _create(
        client, stages=["planner", "architect"], approval_gates=["plan"]
    )["run_id"]
    _wait_status(client, run_id, "waiting_approval")

    started = time.monotonic()
    body = client.post(f"/api/runs/{run_id}/cancel").json()
    assert body["cancelled"] is True

    final = _wait_terminal(client, run_id)
    elapsed = time.monotonic() - started
    assert final["status"] == "cancelled"
    assert final["board"]["status"] == "cancelled"
    # the gate is woken by an Event, not by a poll or a timeout
    assert elapsed < 5.0, f"cancel took {elapsed:.1f}s — the gate was polled, not woken"
    # and the slot is released for the next run
    again = _create(client, stages=["planner"], no_run_tests=True)["run_id"]
    assert _wait_terminal(client, again)["status"] == "succeeded"


def test_execution_gate_shows_command_cwd_and_timeout_before_approving(
    client_factory, tmp_path: Path
) -> None:
    command = 'python -c "print(42)"'
    reply = (
        '{"run_command": "python -c \\"print(42)\\"", '
        '"files": [{"path": "tests/test_x.py", "content": "def test_ok(): pass\\n"}]}'
    )
    client = client_factory(
        runtime_factory=mock_factory(tmp_path, replies={"tester": reply})
    )
    run_id = _create(
        client, stages=["tester"], no_run_tests=False, approval_gates=["execution"]
    )["run_id"]

    waiting = _wait_status(client, run_id, "waiting_approval")
    gate = waiting["gate"]
    assert gate["gate"] == "execution"
    payload = gate["payload"]
    assert payload["command"] == command
    assert payload["cwd"].replace("\\", "/").endswith(
        "workspace/build-a-tiny-calculator"
    )
    assert payload["timeout_seconds"] == 300  # the exact timeout being approved

    client.post(f"/api/runs/{run_id}/approve")
    final = _wait_terminal(client, run_id)
    # only after approval did the command actually run, and it passed
    assert final["status"] == "succeeded"
    assert final["tests"] is not None and final["tests"]["ok"] is True
    assert final["tests"]["command"] == command


def test_approve_and_reject_recheck_the_token_at_the_call_site(
    client: TestClient,
) -> None:
    """D17: the gate endpoints must never answer without the token."""
    run_id = _create(client, stages=["planner"], approval_gates=["plan"])["run_id"]
    _wait_status(client, run_id, "waiting_approval")

    token = client.headers.pop("X-API-Key")
    try:
        assert client.post(f"/api/runs/{run_id}/approve").status_code == 401
        assert (
            client.post(f"/api/runs/{run_id}/reject", json={"note": "no"}).status_code
            == 401
        )
    finally:
        client.headers["X-API-Key"] = token
    # the forged attempts changed nothing: still waiting, still undecided
    state = client.get(f"/api/runs/{run_id}").json()
    assert state["status"] == "waiting_approval"
    assert state["gate"]["decision"] is None

    client.post(f"/api/runs/{run_id}/approve")
    assert _wait_terminal(client, run_id)["status"] == "succeeded"


def test_approve_on_a_run_that_is_not_waiting_is_409(client: TestClient) -> None:
    run_id = _create(client, stages=["planner"], no_run_tests=True)["run_id"]
    _wait_terminal(client, run_id)
    assert client.post(f"/api/runs/{run_id}/approve").status_code == 409
    assert client.post(f"/api/runs/{run_id}/reject", json={}).status_code == 409
    assert client.post("/api/runs/nope/approve").status_code == 404
    assert client.post("/api/runs/nope/reject").status_code == 404


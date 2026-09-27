"""Terminal HTTP routes (Part B, terminal tab; D43): status, submit a
command, busy/idle, one line only, restart, and 404 before a run has a
project folder.

Like ``test_workspace_terminal.py``, these spawn a real ``powershell.exe`` and
so only run on Windows.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.tests.test_api_runs import TOKEN, _create, _wait_terminal, mock_factory

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="spawns a real powershell.exe")

_TIMEOUT = 20.0


@pytest.fixture
def client(tmp_path: Path):
    app = create_app(root=tmp_path, token=TOKEN, runtime_factory=mock_factory(tmp_path))
    with TestClient(app, base_url="http://127.0.0.1:8765") as test_client:
        test_client.headers.update({"X-API-Key": TOKEN})
        yield test_client


@pytest.fixture
def run_id(client: TestClient) -> str:
    """A finished mock run with a real project folder (empty is fine here;
    the terminal doesn't care what's on disk, only that the folder exists)."""
    rid = _create(client, stages=["tester"], project="term")["run_id"]
    assert _wait_terminal(client, rid)["status"] == "succeeded"
    return rid


def _status(client: TestClient, run_id: str) -> dict:
    response = client.get(f"/api/runs/{run_id}/terminal")
    assert response.status_code == 200, response.text
    return response.json()


def _wait_idle(client: TestClient, run_id: str, *, timeout: float = _TIMEOUT) -> dict:
    deadline = time.monotonic() + timeout
    status = _status(client, run_id)
    while status["busy"] and time.monotonic() < deadline:
        time.sleep(0.2)
        status = _status(client, run_id)
    assert not status["busy"], f"terminal for {run_id} never went idle: {status}"
    return status


def test_status_starts_a_shell_in_the_project_folder(client: TestClient, run_id: str) -> None:
    status = _status(client, run_id)
    assert status["busy"] is False
    assert status["closed"] is False
    assert status["start_error"] is None
    assert status["cwd"].endswith("term")


def test_a_command_runs_and_the_terminal_goes_idle_again(client: TestClient, run_id: str) -> None:
    before = _status(client, run_id)["last_seq"]
    response = client.post(f"/api/runs/{run_id}/terminal/input", json={"command": "Write-Output hi"})
    assert response.status_code == 202
    status = _wait_idle(client, run_id)
    assert status["last_seq"] > before


def test_a_second_command_while_busy_is_409(client: TestClient, run_id: str) -> None:
    response = client.post(f"/api/runs/{run_id}/terminal/input", json={"command": "Start-Sleep -Seconds 2"})
    assert response.status_code == 202
    try:
        response = client.post(f"/api/runs/{run_id}/terminal/input", json={"command": "Write-Output too-soon"})
        assert response.status_code == 409
    finally:
        _wait_idle(client, run_id)


def test_a_multiline_command_is_422(client: TestClient, run_id: str) -> None:
    response = client.post(
        f"/api/runs/{run_id}/terminal/input", json={"command": "Write-Output a\nWrite-Output b"}
    )
    assert response.status_code == 422


def test_an_empty_command_is_422(client: TestClient, run_id: str) -> None:
    response = client.post(f"/api/runs/{run_id}/terminal/input", json={"command": "   "})
    assert response.status_code == 422


def test_restart_kills_the_shell_and_a_new_command_still_works(client: TestClient, run_id: str) -> None:
    client.post(f"/api/runs/{run_id}/terminal/input", json={"command": "Start-Sleep -Seconds 30"})
    assert _status(client, run_id)["busy"] is True
    assert client.post(f"/api/runs/{run_id}/terminal/restart").status_code == 202
    deadline = time.monotonic() + _TIMEOUT
    while _status(client, run_id)["busy"] and time.monotonic() < deadline:
        time.sleep(0.2)
    response = client.post(f"/api/runs/{run_id}/terminal/input", json={"command": "Write-Output back"})
    assert response.status_code == 202
    _wait_idle(client, run_id)


def test_interrupt_is_a_no_op_when_nothing_is_running(client: TestClient, run_id: str) -> None:
    assert client.post(f"/api/runs/{run_id}/terminal/interrupt").status_code == 202


def test_terminal_routes_404_before_the_project_exists(client: TestClient) -> None:
    # Mirrors test_api_files.py's queued-run fixture: the second run never
    # starts while the single worker is held by the first, so it has no
    # project folder yet.
    blocker = _create(client, stages=["planner"], approval_gates=["plan"])["run_id"]
    queued = _create(client, stages=["tester"])["run_id"]
    try:
        assert client.get(f"/api/runs/{queued}/terminal").status_code == 404
        body = {"command": "dir"}
        assert client.post(f"/api/runs/{queued}/terminal/input", json=body).status_code == 404
    finally:
        client.post(f"/api/runs/{blocker}/cancel")
        client.post(f"/api/runs/{queued}/cancel")


def test_unknown_run_is_404(client: TestClient) -> None:
    assert client.get("/api/runs/no-such-run/terminal").status_code == 404

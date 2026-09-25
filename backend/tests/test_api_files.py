"""Run file routes (Part B, B4; D30): list and read, read-only, sandboxed.

A mock run writes a real manifest into its own project folder; the edge cases
then add files there directly. Nothing touches the network.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.workspace_files import MAX_LISTED_FILES, MAX_VIEW_BYTES
from backend.tests.test_api_runs import TOKEN, _create, _wait_terminal, mock_factory

TESTER_REPLY = (
    '{"run_command": "python -m pytest -q", "files": ['
    '{"path": "tests/test_x.py", "content": "def test_ok():\\n    assert 1 + 1 == 2\\n"},'
    '{"path": "src/calc.py", "content": "def add(a, b):\\n    return a + b\\n"}]}'
)


@pytest.fixture
def client(tmp_path: Path):
    app = create_app(
        root=tmp_path,
        token=TOKEN,
        runtime_factory=mock_factory(tmp_path, replies={"tester": TESTER_REPLY}),
    )
    with TestClient(app, base_url="http://127.0.0.1:8765") as test_client:
        test_client.headers.update({"X-API-Key": TOKEN})
        yield test_client


@pytest.fixture
def finished(client: TestClient) -> tuple[str, Path]:
    """A finished mock run whose tester wrote two files: (run_id, project folder)."""
    run_id = _create(client, stages=["tester"], project="calc")["run_id"]
    assert _wait_terminal(client, run_id)["status"] == "succeeded"
    folder = client.app.state.run_manager.project_dir(run_id)
    assert folder is not None and folder.is_dir()
    return run_id, folder


def read(client: TestClient, run_id: str, path: str):
    return client.get(f"/api/runs/{run_id}/files/content", params={"path": path})


def test_list_shows_what_the_run_wrote_on_disk(client: TestClient, finished) -> None:
    run_id, _folder = finished
    body = client.get(f"/api/runs/{run_id}/files").json()
    assert body["project"] == "calc"
    assert [f["path"] for f in body["files"]] == ["src/calc.py", "tests/test_x.py"]
    assert all(f["size"] > 0 for f in body["files"])
    assert body["truncated"] is False


def test_read_returns_the_exact_text(client: TestClient, finished) -> None:
    run_id, _folder = finished
    body = read(client, run_id, "src/calc.py").json()
    assert body == {
        "run_id": run_id,
        "path": "src/calc.py",
        "size": len("def add(a, b):\n    return a + b\n"),
        "content": "def add(a, b):\n    return a + b\n",
        "binary": False,
        "truncated": False,
    }
    # backslashes are accepted and shown normalised
    assert read(client, run_id, "src\\calc.py").json()["path"] == "src/calc.py"


@pytest.mark.parametrize(
    "path",
    [
        "../calc/src/calc.py",
        "src/../../x.txt",
        "/etc/passwd",
        "C:\\Windows\\win.ini",
        "C:/Windows/win.ini",
        "\\\\server\\share\\x",
        "src/\x00calc.py",
        "CON",
        "src/nul.txt",
    ],
)
def test_paths_that_leave_the_project_are_refused(
    client: TestClient, finished, path: str
) -> None:
    run_id, _folder = finished
    assert read(client, run_id, path).status_code == 400


def test_a_link_pointing_outside_the_project_is_refused_and_not_listed(
    client: TestClient, finished, tmp_path: Path
) -> None:
    run_id, folder = finished
    secret = tmp_path / "outside-secret.txt"
    secret.write_text("TOP SECRET", encoding="utf-8")
    try:
        os.symlink(secret, folder / "leak.txt")
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks needs Developer Mode or admin on Windows")
    assert read(client, run_id, "leak.txt").status_code == 400
    listed = [f["path"] for f in client.get(f"/api/runs/{run_id}/files").json()["files"]]
    assert "leak.txt" not in listed


@pytest.mark.skipif(sys.platform != "win32", reason="directory junctions are Windows-only")
def test_a_junction_pointing_outside_the_project_is_refused_and_not_listed(
    client: TestClient, finished, tmp_path: Path
) -> None:
    """Junctions need no special rights on Windows, so this is the real risk there."""
    import _winapi

    run_id, folder = finished
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("TOP SECRET", encoding="utf-8")
    _winapi.CreateJunction(str(outside), str(folder / "escape"))
    assert read(client, run_id, "escape/secret.txt").status_code == 400
    listed = client.get(f"/api/runs/{run_id}/files").text
    assert "secret.txt" not in listed and "TOP SECRET" not in listed


def test_missing_files_folders_and_runs_are_404(client: TestClient, finished) -> None:
    run_id, _folder = finished
    assert read(client, run_id, "nope.py").status_code == 404
    assert read(client, run_id, "src").status_code == 404  # a folder, not a file
    assert read(client, "no-such-run", "a.py").status_code == 404
    assert client.get("/api/runs/no-such-run/files").status_code == 404


def test_large_files_are_capped_and_binary_files_are_not_decoded(
    client: TestClient, finished
) -> None:
    run_id, folder = finished
    # "é" is two bytes: put one across the cap to prove the cut never breaks text
    (folder / "big.txt").write_bytes(b"a" * (MAX_VIEW_BYTES - 1) + "é".encode() + b"tail")
    body = read(client, run_id, "big.txt").json()
    assert body["truncated"] is True and body["binary"] is False
    assert body["size"] == MAX_VIEW_BYTES + 5
    assert body["content"] == "a" * (MAX_VIEW_BYTES - 1)

    (folder / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
    body = read(client, run_id, "logo.png").json()
    assert body["binary"] is True and body["content"] is None

    (folder / "latin1.txt").write_bytes("caf\xe9".encode("latin-1"))
    assert read(client, run_id, "latin1.txt").json()["binary"] is True


def test_listing_skips_tool_folders_and_is_capped(client: TestClient, finished) -> None:
    run_id, folder = finished
    for skipped in ("node_modules/pkg/index.js", ".git/HEAD", "__pycache__/x.pyc", ".venv/x"):
        target = folder / skipped
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
    listed = [f["path"] for f in client.get(f"/api/runs/{run_id}/files").json()["files"]]
    assert listed == ["src/calc.py", "tests/test_x.py"]

    many = folder / "many"
    many.mkdir()
    for i in range(MAX_LISTED_FILES + 5):
        (many / f"f{i:04}.txt").write_text("x", encoding="utf-8")
    body = client.get(f"/api/runs/{run_id}/files").json()
    assert body["truncated"] is True and len(body["files"]) == MAX_LISTED_FILES


def test_a_queued_run_has_no_files_yet(client: TestClient) -> None:
    blocker = _create(client, stages=["planner"], approval_gates=["plan"])["run_id"]
    queued = _create(client, stages=["tester"])["run_id"]
    try:
        assert client.get(f"/api/runs/{queued}/files").json()["files"] == []
        assert read(client, queued, "a.py").status_code == 404
    finally:
        client.post(f"/api/runs/{blocker}/cancel")
        client.post(f"/api/runs/{queued}/cancel")


def test_file_routes_are_read_only(client: TestClient, finished) -> None:
    run_id, _folder = finished
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        url = f"/api/runs/{run_id}/files/content"
        response = client.request(method, url, params={"path": "src/calc.py"})
        assert response.status_code == 405

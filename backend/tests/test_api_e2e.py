"""End-to-end API tests (Part A step 6): a run driven the way the UI drives it.

A real uvicorn server on an ephemeral 127.0.0.1 port, a real ``httpx`` client
reading the SSE stream incrementally, and approval decisions made *in reaction
to* ``api.run_waiting_approval`` events on that stream. The pipeline runs on
offline mock providers, so nothing leaves the machine and no quota is spent.

Also here: the security pass over the whole surface (every route needs the
token, the bind is loopback-only, no response ever contains a key value).
"""

from __future__ import annotations

import io
import json
import secrets
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from backend.api import __main__ as launcher
from backend.api.app import create_app
from backend.core.config import PACKAGE_ROOT
from backend.tests.test_api_runs import TOKEN, mock_factory

COMMAND = 'python -c "print(42)"'
TESTER_REPLY = (
    '{"run_command": "python -c \\"print(42)\\"", '
    '"files": [{"path": "tests/test_x.py", "content": "def test_ok(): pass\\n"}]}'
)
ALL_GATES = ["plan", "architecture", "execution"]
FAKE_KEYS = {
    "GEMINI_API_KEY": "AIza-e2e-gemini-secret-0123456789",
    "GROQ_API_KEY": "gsk_e2e-groq-secret-0123456789",
    # deploy credentials must never come back either (D41)
    "GITHUB_TOKEN": "github_pat_e2e_" + "g" * 30,
    "RENDER_API_KEY": "rnd_e2e" + "r" * 24,
    # and the web research key (D42)
    "FIRECRAWL_API_KEY": "fc-e2e" + "f" * 26,
}

#: The whole API surface (Part A, the B4 file routes, the P5.5 deploy routes). A new route fails
#: test_the_api_surface_is_pinned until it is added here, tested, and
#: documented in the README.
SURFACE = {
    ("GET", "/api/health"),
    ("GET", "/api/events"),
    ("GET", "/api/events/stream"),
    ("POST", "/api/runs"),
    ("GET", "/api/runs"),
    ("GET", "/api/runs/{run_id}"),
    ("POST", "/api/runs/{run_id}/cancel"),
    ("POST", "/api/runs/{run_id}/approve"),
    ("POST", "/api/runs/{run_id}/reject"),
    ("GET", "/api/runs/{run_id}/files"),
    ("GET", "/api/runs/{run_id}/files/content"),
    ("GET", "/api/runs/{run_id}/deploy/preview"),
    ("POST", "/api/runs/{run_id}/deploy"),
    ("GET", "/api/runs/{run_id}/deploys"),
    ("GET", "/api/settings"),
    ("PUT", "/api/settings"),
    ("PUT", "/api/settings/keys"),
    ("GET", "/api/projects"),
}


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway project root with the tracked config and no real keys."""
    shutil.copytree(PACKAGE_ROOT / "config", tmp_path / "config")
    for name in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY",
                 "GITHUB_TOKEN", "RENDER_API_KEY", "RENDER_OWNER_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(sys.modules, "keyring", None)
    return tmp_path


@pytest.fixture
def server(root: Path) -> Iterator[str]:
    """Serve the app on 127.0.0.1:<ephemeral>; yields the base URL."""
    app = create_app(
        root=root,
        token=TOKEN,
        runtime_factory=mock_factory(root, replies={"tester": TESTER_REPLY}),
    )
    config = uvicorn.Config(app, host=launcher.BIND_HOST, port=0, log_level="warning")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not srv.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.02)
    port = srv.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(10)


@pytest.fixture
def http(server: str) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=server, headers={"X-API-Key": TOKEN}, timeout=30) as client:
        yield client


def create_run(http: httpx.Client, **body: Any) -> str:
    payload = {"request": "build a tiny calculator", "no_run_tests": False, **body}
    response = http.post("/api/runs", json=payload)
    assert response.status_code == 202, response.text
    return response.json()["run_id"]


def follow(
    http: httpx.Client, run_id: str, on_gate: Callable[[dict], None]
) -> list[dict]:
    """Read the stream like the UI: decode frames as they arrive, react to gates.

    Returns every event; returns at all only because the server closed the
    stream after the run's closing event.
    """
    events: list[dict] = []
    with http.stream("GET", "/api/events/stream", params={"run_id": run_id}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        for line in response.iter_lines():
            if not line.startswith("data: "):
                continue
            event = json.loads(line[len("data: "):])
            events.append(event)
            if event["kind"] == "api.run_waiting_approval":
                on_gate(event)
    return events


def kinds(events: list[dict]) -> list[str]:
    return [e["kind"] for e in events]


def assert_same_as_replay(http: httpx.Client, run_id: str, events: list[dict]) -> None:
    """The live stream saw exactly what was persisted: no gap, no repeat."""
    assert [e["seq"] for e in events] == list(range(len(events)))
    page = http.get("/api/events", params={"run_id": run_id}).json()
    assert page["events"] == events
    assert page["has_more"] is False


# -- the UI flow -------------------------------------------------------------
def test_full_run_approving_every_gate_through_the_api(http: httpx.Client) -> None:
    run_id = create_run(http, approval_gates=ALL_GATES)
    seen: list[str] = []

    def approve(event: dict) -> None:
        gate = event["data"]["gate"]
        seen.append(gate)
        state = http.get(f"/api/runs/{run_id}").json()
        assert state["status"] == "waiting_approval"
        assert state["gate"]["gate"] == gate
        if gate == "execution":
            # the exact command is shown before anything runs
            assert state["gate"]["payload"]["command"] == COMMAND
            assert state["tests"] is None
        response = http.post(f"/api/runs/{run_id}/approve", json={"note": f"ok {gate}"})
        assert response.status_code == 200, response.text

    events = follow(http, run_id, approve)

    assert seen == ALL_GATES
    order = kinds(events)
    assert order[0] == "api.run_queued"
    assert order[-1] == "api.run_succeeded"
    assert order.count("api.run_approved") == 3
    for i, kind in enumerate(order):
        if kind == "api.run_waiting_approval":
            assert order[i + 1] == "api.run_approved"  # nothing ran past a gate
    assert_same_as_replay(http, run_id, events)

    final = http.get(f"/api/runs/{run_id}").json()
    assert final["status"] == "succeeded"
    assert final["tests"]["ok"] is True and final["tests"]["command"] == COMMAND
    assert [a["stage"] for a in final["agents"]][:2] == ["planner", "architect"]
    listed = http.get("/api/runs").json()
    assert listed["runs"][0]["run_id"] == run_id and listed["active_run_id"] is None
    assert http.get("/api/projects").status_code == 200


def test_full_run_rejected_at_the_architecture_gate(http: httpx.Client) -> None:
    run_id = create_run(http, approval_gates=ALL_GATES)

    def decide(event: dict) -> None:
        action = "approve" if event["data"]["gate"] == "plan" else "reject"
        body = {"note": "use SQLite instead"} if action == "reject" else None
        assert http.post(f"/api/runs/{run_id}/{action}", json=body).status_code == 200

    events = follow(http, run_id, decide)

    order = kinds(events)
    assert order[-1] == "api.run_failed"
    assert order.count("api.run_waiting_approval") == 2  # never reached execution
    assert order.index("api.run_rejected") > order.index("api.run_approved")
    assert events[-1]["data"]["reason"] == "rejected"
    assert_same_as_replay(http, run_id, events)
    final = http.get(f"/api/runs/{run_id}").json()
    assert final["status"] == "failed"
    assert final["result"]["reason"] == "rejected"
    assert "architecture" in final["error"] and "use SQLite instead" in final["error"]
    assert final["tests"] is None  # the command was never run
    # the slot is free: the next run goes straight through
    nxt = create_run(http, stages=["planner"], no_run_tests=True)
    assert kinds(follow(http, nxt, lambda _e: None))[-1] == "api.run_succeeded"


def test_full_run_cancelled_while_waiting_at_the_execution_gate(http: httpx.Client) -> None:
    run_id = create_run(http, approval_gates=ALL_GATES)

    def decide(event: dict) -> None:
        if event["data"]["gate"] == "execution":
            response = http.post(f"/api/runs/{run_id}/cancel")
            assert response.status_code == 200 and response.json()["cancelled"] is True
        else:
            assert http.post(f"/api/runs/{run_id}/approve").status_code == 200

    events = follow(http, run_id, decide)

    order = kinds(events)
    assert order[-1] == "api.run_cancelled"
    assert order.count("api.run_approved") == 2  # plan and architecture
    assert order.index("api.run_cancel_requested") > order.index("api.run_waiting_approval")
    assert_same_as_replay(http, run_id, events)
    final = http.get(f"/api/runs/{run_id}").json()
    assert final["status"] == "cancelled"
    assert final["tests"] is None  # cancelled before the command ran
    # a decision after the fact is refused, not silently applied
    assert http.post(f"/api/runs/{run_id}/approve").status_code == 409
    assert http.post(f"/api/runs/{run_id}/cancel").json()["cancelled"] is False


# -- security pass -------------------------------------------------------------
def test_the_api_surface_is_pinned(root: Path) -> None:
    # the schema is generated in-process only; openapi_url stays None, so it
    # is never served
    paths = create_app(root=root, token=TOKEN).openapi()["paths"]
    assert {(m.upper(), path) for path, ops in paths.items() for m in ops} == SURFACE


@pytest.mark.parametrize(("method", "path"), sorted(SURFACE))
def test_every_api_route_requires_the_token(server: str, method: str, path: str) -> None:
    url = server + path.replace("{run_id}", "20260925-000000-abcd")
    for headers in ({}, {"X-API-Key": "wrong"}):
        params = {"run_id": "x", "path": "a.txt"}
        response = httpx.request(method, url, params=params, json={}, headers=headers)
        assert response.status_code == 401, (method, path, headers)


class FakeServer:
    """Stands in for uvicorn.Server: records the sockets it would serve."""

    bound: list[tuple[str, int]] = []

    def __init__(self, _config: object) -> None:
        FakeServer.bound = []

    def run(self, sockets: list) -> None:
        FakeServer.bound = [s.getsockname() for s in sockets]
        for s in sockets:
            s.close()


@pytest.fixture
def fake_launch(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Run launcher.main without serving; returns the kwargs create_app got."""
    seen: dict = {}
    monkeypatch.setattr(launcher, "create_app", lambda **kw: seen.update(kw) or object())
    monkeypatch.setattr(launcher.uvicorn, "Server", FakeServer)
    return seen


def test_the_launcher_binds_loopback_only(
    fake_launch: dict, capsys: pytest.CaptureFixture[str]
) -> None:
    assert launcher.main(["--port", "0"]) == 0
    host, port = FakeServer.bound[0]
    assert host == "127.0.0.1" and port > 0
    # by hand: the real port (never the requested 0) and a token for curl
    out = capsys.readouterr().out
    assert f"Serving on http://127.0.0.1:{port} " in out
    assert fake_launch["token"] in out
    # there is no flag that could change the host
    with pytest.raises(SystemExit):
        launcher.main(["--host", "0.0.0.0"])


def test_token_stdin_uses_the_shells_token_and_prints_only_the_port(
    fake_launch: dict, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    token = "t" * 43
    monkeypatch.setattr(sys, "stdin", io.StringIO(token + "\n"))
    assert launcher.main(["--port", "0", "--token-stdin"]) == 0
    assert fake_launch["token"] == token
    out = capsys.readouterr().out
    assert out == f'SDA_READY {{"port": {FakeServer.bound[0][1]}}}\n'
    assert token not in out


@pytest.mark.parametrize("line", ["", "short", "has space" + "x" * 40, "x" * 200])
def test_token_stdin_refuses_a_bad_token(
    fake_launch: dict, monkeypatch: pytest.MonkeyPatch, line: str
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(line + "\n"))
    assert launcher.main(["--port", "0", "--token-stdin"]) == 2
    assert not fake_launch  # no app was built, nothing was served


def test_the_desktop_launch_contract_end_to_end(root: Path) -> None:
    """The real process, started the way the Electron shell starts it."""
    token = secrets.token_urlsafe(32)
    proc = subprocess.Popen(
        [sys.executable, "-m", "backend.api", "--port", "0", "--root", str(root),
         "--token-stdin", "--exit-with-stdin"],
        cwd=PACKAGE_ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        proc.stdin.write(token + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        assert line.startswith("SDA_READY ") and token not in line
        base = f"http://127.0.0.1:{json.loads(line.split(' ', 1)[1])['port']}"
        assert httpx.get(base + "/api/health", headers={"X-API-Key": token}).status_code == 200
        assert httpx.get(base + "/api/health").status_code == 401
        proc.stdin.close()  # the shell quits: the lifeline closes
        assert proc.wait(timeout=15) == 0  # a graceful exit, not a kill
    finally:
        if proc.poll() is None:
            proc.kill()


def test_no_response_ever_contains_a_key_value(
    http: httpx.Client, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keys go in; only "set"/"missing" ever comes out, on any route."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-e2e-shell-secret-0123456789")
    secrets_ = [*FAKE_KEYS.values(), "sk-or-e2e-shell-secret-0123456789"]
    bodies: list[str] = []

    put = http.put("/api/settings/keys", json={"keys": FAKE_KEYS})
    assert put.status_code == 200
    assert put.json()["keys"] == {
        **dict.fromkeys(
            (
                "GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY",
                "GITHUB_TOKEN", "RENDER_API_KEY", "FIRECRAWL_API_KEY",
            ),
            "set",
        ),
        "RENDER_OWNER_ID": "missing",
        "CEREBRAS_API_KEY": "missing",
    }
    bodies.append(put.text)
    bad_value = {"GEMINI_API_KEY": FAKE_KEYS["GEMINI_API_KEY"] + " x"}
    bad = http.put("/api/settings/keys", json={"keys": bad_value})
    assert bad.status_code == 422
    bodies.append(bad.text)
    bodies.append(http.put("/api/settings", json={}).text)

    run_id = create_run(http, stages=["planner"], no_run_tests=True)
    bodies.extend(json.dumps(e) for e in follow(http, run_id, lambda _e: None))
    for path in ("/api/health", "/api/settings", "/api/runs", f"/api/runs/{run_id}",
                 "/api/projects", f"/api/events?run_id={run_id}"):
        response = http.get(path)
        assert response.status_code == 200, path
        bodies.append(response.text)

    for body in bodies:
        for secret in secrets_:
            assert secret not in body
    # the key did land where it belongs
    assert FAKE_KEYS["GEMINI_API_KEY"] in (root / ".env").read_text(encoding="utf-8")


def test_deploys_use_real_targets_unless_the_dev_switch_is_set(
    fake_launch: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SDA_FAKE_DEPLOY", raising=False)
    assert launcher.main(["--port", "0"]) == 0
    assert fake_launch["deploy_clients"] is None  # the real GitHub/Render clients

    from backend.tests import fake_targets

    monkeypatch.setenv("SDA_FAKE_DEPLOY", "1")
    assert launcher.main(["--port", "0"]) == 0
    assert fake_launch["deploy_clients"] is fake_targets.clients


def test_the_dev_switch_refuses_to_start_without_the_fakes(
    fake_launch: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An installed app has no backend/tests: the switch must not fall back to real targets."""
    monkeypatch.setenv("SDA_FAKE_DEPLOY", "1")
    monkeypatch.setitem(sys.modules, "backend.tests.fake_targets", None)  # import fails
    assert launcher.main(["--port", "0"]) == 2
    assert not fake_launch  # no app was built

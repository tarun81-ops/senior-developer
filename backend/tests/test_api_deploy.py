"""Phase 5, P5.5: the deploy API, end to end on mock runs and fake targets (D40). Offline."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.repository import SqliteEventRepository
from backend.core.config import get_settings
from backend.core.deploy.github import GitHubClient
from backend.core.deploy.render import RenderClient
from backend.tests.test_api_runs import TOKEN, _create, _wait_terminal, mock_factory
from backend.tests.test_deploy_github import TOKEN as GH_TOKEN
from backend.tests.test_deploy_github import FakeGitHub
from backend.tests.test_deploy_render import KEY as RENDER_KEY
from backend.tests.test_deploy_render import FakeRender

TEST_FILE = {"path": "tests/test_ok.py", "content": "def test_ok():\n    assert True\n"}


def tester_reply(*files: dict) -> str:
    """A tester answer that writes a project and names its (passing) test command."""
    return json.dumps({"run_command": "python -m pytest -q", "files": [TEST_FILE, *files]})


STATIC = tester_reply({"path": "index.html", "content": "<h1>calc</h1>\n"})
BACKEND = tester_reply(
    {"path": "requirements.txt", "content": "fastapi\n"},
    {"path": "app/main.py", "content": "from fastapi import FastAPI\n\napp = FastAPI()\n"},
)


class Targets:
    """Fake GitHub + Render, handed to the app through its deploy_clients seam."""

    def __init__(self) -> None:
        self.github = FakeGitHub()
        self.render = FakeRender()
        self.calls = 0
        self.gate: threading.Event | None = None  # set: clients are held until released

    def __call__(self, target: str):
        self.calls += 1
        if self.gate is not None:
            self.gate.wait(10)
        github = GitHubClient(
            GH_TOKEN, transport=httpx.MockTransport(self.github), sleep=lambda _s: None
        )
        render = RenderClient(
            RENDER_KEY, transport=httpx.MockTransport(self.render), sleep=lambda _s: None
        )
        return github, (render if target == "render" else None)


@pytest.fixture(autouse=True)
def keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", GH_TOKEN)
    monkeypatch.setenv("RENDER_API_KEY", RENDER_KEY)
    monkeypatch.delenv("RENDER_OWNER_ID", raising=False)


@pytest.fixture
def targets() -> Targets:
    return Targets()


def launch(root: Path, targets: Targets, reply: str) -> TestClient:
    app = create_app(
        root=root,
        token=TOKEN,
        runtime_factory=mock_factory(root, replies={"tester": reply}),
        deploy_clients=targets,
    )
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    client.headers.update({"X-API-Key": TOKEN})
    return client


def finished_run(client: TestClient, **options) -> str:
    body = {"stages": ["tester"], "project": "calc", "no_run_tests": False, **options}
    run_id = _create(client, **body)["run_id"]
    state = _wait_terminal(client, run_id)
    assert state["status"] == "succeeded", state
    return run_id


def preview(client: TestClient, run_id: str) -> dict:
    response = client.get(f"/api/runs/{run_id}/deploy/preview")
    assert response.status_code == 200, response.text
    return response.json()


def stream(client: TestClient, stream_id: str) -> list[dict]:
    body = client.get("/api/events/stream", params={"run_id": stream_id}).text
    return [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]


def confirm(client: TestClient, run_id: str, fingerprint: str) -> httpx.Response:
    return client.post(f"/api/runs/{run_id}/deploy", json={"fingerprint": fingerprint})


# -- a static site: preview, confirm, follow, done ---------------------------------------------
def test_a_tested_static_site_previews_then_deploys_to_pages(
    tmp_path: Path, targets: Targets
) -> None:
    with launch(tmp_path, targets, STATIC) as client:
        run_id = finished_run(client)
        shown = preview(client, run_id)
        assert shown["can_deploy"] is True and shown["blockers"] == []
        assert shown["target"]["target"] == "github-pages" and shown["visibility"] == "public"
        assert shown["repo"] == "sda-calc"
        assert shown["expected_url"] == "https://<your GitHub username>.github.io/sda-calc/"
        assert {f["path"] for f in shown["files"]} == {"index.html", "tests/test_ok.py"}
        assert targets.calls == 0  # the preview never reaches GitHub

        started = confirm(client, run_id, shown["fingerprint"])
        assert started.status_code == 202, started.text
        deploy_id = started.json()["deploy_id"]
        events = stream(client, deploy_id)  # returns once the deploy's stream closes

        assert events[0]["kind"] == "deploy.queued" and events[-1]["kind"] == "deploy.succeeded"
        assert events[-1]["message"] == "https://octo.github.io/sda-calc/"
        record = client.get(f"/api/runs/{run_id}/deploys").json()["deploys"][0]
        assert (
            record["status"] == "succeeded" and record["url"] == "https://octo.github.io/sda-calc/"
        )
        assert set(targets.github.files("sda-calc")) >= {"index.html", "tests/test_ok.py"}


def test_a_tested_python_backend_deploys_to_render(tmp_path: Path, targets: Targets) -> None:
    with launch(tmp_path, targets, BACKEND) as client:
        run_id = finished_run(client)
        shown = preview(client, run_id)
        assert shown["can_deploy"], shown["blockers"]
        assert (shown["target"]["target"], shown["visibility"]) == ("render", "private")
        assert (
            shown["target"]["start_command"] == "uvicorn app.main:app --host 0.0.0.0 --port $PORT"
        )
        deploy_id = confirm(client, run_id, shown["fingerprint"]).json()["deploy_id"]
        events = stream(client, deploy_id)
        assert events[-1]["kind"] == "deploy.succeeded"
        assert events[-1]["message"] == "https://sda-calc.onrender.com"
        assert targets.github.repos["sda-calc"]["data"]["private"] is True


# -- nothing is published that wasn't shown or isn't allowed ------------------------------------
def test_a_project_changed_after_the_preview_is_refused(tmp_path: Path, targets: Targets) -> None:
    with launch(tmp_path, targets, STATIC) as client:
        run_id = finished_run(client)
        shown = preview(client, run_id)
        folder = client.app.state.run_manager.project_dir(run_id)
        (folder / "index.html").write_text("<h1>edited after the preview</h1>", encoding="utf-8")

        refused = confirm(client, run_id, shown["fingerprint"])
        assert (
            refused.status_code == 409 and "changed since the preview" in refused.json()["detail"]
        )
        assert targets.calls == 0 and targets.github.requests == []
        # a fresh preview shows the new content and deploys fine
        again = preview(client, run_id)
        assert again["fingerprint"] != shown["fingerprint"]
        assert confirm(client, run_id, again["fingerprint"]).status_code == 202


@pytest.mark.parametrize(
    ("options", "blocker"),
    [
        ({"no_run_tests": True}, "tests were skipped"),
        # succeeded, but no test ever ran (no tester stage). A dry run can't be
        # reached here: it writes nothing, so its tests find nothing and it
        # fails; the dry-run rule itself is unit-tested in P5.1.
        ({"stages": ["planner"]}, "no tests ran"),
    ],
)
def test_runs_without_passing_tests_cannot_deploy(
    tmp_path: Path, targets: Targets, options: dict, blocker: str
) -> None:
    with launch(tmp_path, targets, STATIC) as client:
        body = {"stages": ["tester"], "project": "calc", "no_run_tests": False, **options}
        run_id = _create(client, **body)["run_id"]
        _wait_terminal(client, run_id)
        shown = preview(client, run_id)
        assert shown["can_deploy"] is False and any(blocker in b for b in shown["blockers"])
        refused = confirm(client, run_id, shown["fingerprint"])
        assert refused.status_code == 409 and blocker in refused.json()["detail"]
        assert targets.calls == 0


def test_a_key_in_the_project_blocks_the_deploy_without_showing_it(
    tmp_path: Path, targets: Targets
) -> None:
    leaked = "gsk_" + "z" * 40
    reply = tester_reply(
        {"path": "index.html", "content": "<h1>x</h1>"},
        {"path": "config.js", "content": f"const key = '{leaked}';\n"},
    )
    with launch(tmp_path, targets, reply) as client:
        run_id = finished_run(client)
        response = client.get(f"/api/runs/{run_id}/deploy/preview")
        shown = response.json()
        assert shown["can_deploy"] is False
        assert shown["findings"] == [{"path": "config.js", "line": 1, "kind": "Groq key"}]
        assert leaked not in response.text
        assert confirm(client, run_id, shown["fingerprint"]).status_code == 409


def test_missing_keys_are_named_and_block(tmp_path: Path, targets: Targets, monkeypatch) -> None:
    monkeypatch.delenv("RENDER_API_KEY")
    with launch(tmp_path, targets, BACKEND) as client:
        run_id = finished_run(client)
        shown = preview(client, run_id)
        assert shown["missing_keys"] == ["RENDER_API_KEY"] and not shown["can_deploy"]
        assert "set RENDER_API_KEY in Settings first" in shown["blockers"]


def test_one_deploy_at_a_time_per_project(tmp_path: Path, targets: Targets) -> None:
    targets.gate = threading.Event()
    with launch(tmp_path, targets, STATIC) as client:
        run_id = finished_run(client)
        fingerprint = preview(client, run_id)["fingerprint"]
        first = confirm(client, run_id, fingerprint)
        assert first.status_code == 202
        second = confirm(client, run_id, fingerprint)
        assert second.status_code == 409 and "already being deployed" in second.json()["detail"]
        targets.gate.set()
        assert stream(client, first.json()["deploy_id"])[-1]["kind"] == "deploy.succeeded"


# -- failures, streams, history, secrecy -----------------------------------------------------------
def test_a_failed_deploy_ends_its_own_stream_and_leaves_the_run_alone(
    tmp_path: Path, targets: Targets
) -> None:
    targets.github.run_outcome = "failure"
    with launch(tmp_path, targets, STATIC) as client:
        run_id = finished_run(client)
        run_events = stream(client, run_id)
        deploy_id = confirm(client, run_id, preview(client, run_id)["fingerprint"]).json()[
            "deploy_id"
        ]
        events = stream(client, deploy_id)
        assert events[-1]["kind"] == "deploy.failed" and "ended 'failure'" in events[-1]["message"]
        record = client.get(f"/api/runs/{run_id}/deploys").json()["deploys"][0]
        assert record["status"] == "failed" and "ended 'failure'" in record["error"]
        # the run's stream is untouched: same events, still ending on its closing event
        assert stream(client, run_id) == run_events
        assert run_events[-1]["kind"] == "api.run_succeeded"


def test_deploy_history_survives_a_restart_and_interrupted_deploys_say_so(
    tmp_path: Path, targets: Targets
) -> None:
    with launch(tmp_path, targets, STATIC) as client:
        run_id = finished_run(client)
        deploy_id = confirm(client, run_id, preview(client, run_id)["fingerprint"]).json()[
            "deploy_id"
        ]
        stream(client, deploy_id)
    # a deploy that was mid-flight when the app died
    repo = SqliteEventRepository(get_settings(tmp_path).db_path)
    repo.save_deploy(
        "deploy-crashed",
        run_id,
        "2099-01-01T00:00:00+00:00",
        {
            "deploy_id": "deploy-crashed",
            "run_id": run_id,
            "project": "calc",
            "target": "github-pages",
            "fingerprint": "0" * 64,
            "status": "running",
            "url": None,
            "error": None,
            "created_at": "2099-01-01T00:00:00+00:00",
            "started_at": None,
            "finished_at": None,
        },
    )

    with launch(tmp_path, targets, STATIC) as client:
        deploys = client.get(f"/api/runs/{run_id}/deploys").json()["deploys"]
        assert [d["deploy_id"] for d in deploys] == ["deploy-crashed", deploy_id]
        assert deploys[0]["status"] == "failed" and "Interrupted" in deploys[0]["error"]
        assert deploys[1]["status"] == "succeeded"
        assert stream(client, "deploy-crashed")[-1]["kind"] == "deploy.failed"


def test_unknown_runs_are_404(tmp_path: Path, targets: Targets) -> None:
    with launch(tmp_path, targets, STATIC) as client:
        assert client.get("/api/runs/nope/deploy/preview").status_code == 404
        assert confirm(client, "nope", "a" * 64).status_code == 404
        assert client.get("/api/runs/nope/deploys").status_code == 404


def test_no_token_or_key_ever_appears_in_responses_or_events(
    tmp_path: Path, targets: Targets
) -> None:
    with launch(tmp_path, targets, BACKEND) as client:
        run_id = finished_run(client)
        texts = [client.get(f"/api/runs/{run_id}/deploy/preview").text]
        started = confirm(client, run_id, preview(client, run_id)["fingerprint"])
        texts += [
            started.text,
            json.dumps(stream(client, started.json()["deploy_id"])),
            client.get(f"/api/runs/{run_id}/deploys").text,
        ]
        for text in texts:
            assert GH_TOKEN not in text and RENDER_KEY not in text

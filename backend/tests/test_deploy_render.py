"""Phase 5, P5.4: Python backends to Render, against fake Render + fake GitHub (D39). Offline."""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest

from backend.core.deploy.detect import Detection
from backend.core.deploy.github import DeployError, GitHubClient
from backend.core.deploy.render import RenderClient, deploy_backend
from backend.tests.test_deploy_github import TOKEN, FakeGitHub

KEY = "rnd_" + "k" * 24  # fake, built at runtime
API = Detection(
    kind="python",
    target="render",
    framework="fastapi",
    reason="",
    build_command="pip install -r requirements.txt && pip install uvicorn",
    start_command="uvicorn app.main:app --host 0.0.0.0 --port $PORT",
)
FILES = {
    "requirements.txt": b"fastapi\n",
    "app/main.py": b"from fastapi import FastAPI\napp = FastAPI()\n",
}


class FakeRender:
    """The parts of api.render.com/v1 used, shaped like its published OpenAPI."""

    def __init__(
        self, *, owners: int = 1, outcome: str = "live", reachable_repos: set | None = None
    ) -> None:
        self.owners = [
            {"id": f"tea-{i}", "name": f"team {i}", "type": "team"} for i in range(owners)
        ]
        self.outcome = outcome
        self.reachable = reachable_repos  # None: Render's GitHub app sees every repo
        self.services: dict[str, dict] = {}
        self.deploys: dict[str, dict] = {}
        self.requests: list[httpx.Request] = []
        self.fail: dict[tuple[str, str], httpx.Response] = {}

    def add_service(
        self,
        name: str,
        repo: str,
        *,
        build: str = API.build_command,
        start: str = API.start_command,
        status: str = "live",
    ) -> dict:
        sid = f"srv-{len(self.services) + 1}"
        service = {
            "id": sid,
            "name": name,
            "repo": repo,
            "ownerId": "tea-0",
            "type": "web_service",
            "dashboardUrl": f"https://dashboard.render.com/web/{sid}",
            "serviceDetails": {
                "url": f"https://{name}.onrender.com",
                "envSpecificDetails": {"buildCommand": build, "startCommand": start},
            },
        }
        self.services[sid] = service
        self._deploy(sid, commit=None, status=status)
        return service

    def _deploy(self, sid: str, *, commit: str | None, status: str = "created") -> dict:
        deploy = {
            "id": f"dep-{len(self.deploys) + 1}",
            "service": sid,
            "commit": commit,
            "status": status,
            "polls": 0,
        }
        self.deploys[deploy["id"]] = deploy
        return deploy

    def writes(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests if r.method != "GET"]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        if key in self.fail:
            return self.fail[key]
        body = json.loads(request.content) if request.content else {}
        query = parse_qs(request.url.query.decode())
        parts = request.url.path.removeprefix("/v1").strip("/").split("/")
        if parts == ["owners"]:
            return httpx.Response(200, json=[{"owner": o, "cursor": "c"} for o in self.owners])
        if parts == ["services"] and request.method == "GET":
            rows = [
                s for s in self.services.values() if s["name"] in query.get("name", [s["name"]])
            ]
            return httpx.Response(200, json=[{"service": s, "cursor": "c"} for s in rows])
        if parts == ["services"] and request.method == "POST":
            if self.reachable is not None and body["repo"] not in self.reachable:
                return httpx.Response(
                    400, json={"message": f"could not access repo {body['repo']}"}
                )
            details = body["serviceDetails"]
            service = self.add_service(
                body["name"],
                body["repo"],
                build=details["envSpecificDetails"]["buildCommand"],
                start=details["envSpecificDetails"]["startCommand"],
                status="created",
            )
            service["created_with"] = body
            first = next(d for d in self.deploys.values() if d["service"] == service["id"])
            return httpx.Response(201, json={"service": service, "deployId": first["id"]})
        sid = parts[1] if len(parts) > 1 else ""
        if sid not in self.services:
            return httpx.Response(404, json={"message": "not found"})
        if len(parts) == 2 and request.method == "PATCH":
            env = body["serviceDetails"]["envSpecificDetails"]
            self.services[sid]["serviceDetails"]["envSpecificDetails"].update(env)
            return httpx.Response(200, json=self.services[sid])
        if parts[2:] == ["deploys"] and request.method == "POST":
            deploy = self._deploy(sid, commit=body["commitId"])
            return httpx.Response(201, json={k: deploy[k] for k in ("id", "status")})
        if parts[2:] == ["deploys"]:
            mine = [d for d in self.deploys.values() if d["service"] == sid]
            return httpx.Response(
                200,
                json=[{"deploy": {"status": d["status"]}, "cursor": "c"} for d in reversed(mine)][
                    : int(query.get("limit", ["20"])[0])
                ],
            )
        if parts[2] == "deploys" and len(parts) == 4:
            deploy = self.deploys[parts[3]]
            deploy["polls"] += 1  # created -> build_in_progress -> outcome, as polled
            if deploy["status"] in ("created", "build_in_progress"):
                deploy["status"] = "build_in_progress" if deploy["polls"] < 3 else self.outcome
            return httpx.Response(200, json={"id": deploy["id"], "status": deploy["status"]})
        return httpx.Response(404, json={"message": "not found"})


@pytest.fixture
def github() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def render() -> FakeRender:
    return FakeRender()


def deploy(
    github: FakeGitHub,
    render: FakeRender,
    files: dict = FILES,
    detection: Detection = API,
    owner_id: str = "",
) -> tuple[str, list]:
    events: list[tuple[str, str]] = []
    url = deploy_backend(
        GitHubClient(TOKEN, transport=httpx.MockTransport(github), sleep=lambda _s: None),
        RenderClient(
            KEY, owner_id=owner_id, transport=httpx.MockTransport(render), sleep=lambda _s: None
        ),
        name="sda-api",
        detection=detection,
        files=sorted(files.items()),
        emit=lambda k, m: events.append((k, m)),
    )
    return url, events


# -- first deploy -----------------------------------------------------------------------
def test_first_deploy_creates_a_private_repo_and_an_always_on_service(
    github: FakeGitHub, render: FakeRender
) -> None:
    url, events = deploy(github, render)

    assert url == "https://sda-api.onrender.com"
    assert github.repos["sda-api"]["data"]["private"] is True  # D36: backends are private
    assert set(github.files("sda-api")) == {"requirements.txt", "app/main.py"}
    created = next(iter(render.services.values()))["created_with"]
    assert created == {
        "type": "web_service",
        "name": "sda-api",
        "ownerId": "tea-0",
        "repo": "https://github.com/octo/sda-api",
        "branch": "main",
        "autoDeploy": "no",
        "serviceDetails": {
            "runtime": "python",
            "plan": "free",
            "region": "oregon",
            "envSpecificDetails": {
                "buildCommand": "pip install -r requirements.txt && pip install uvicorn",
                "startCommand": "uvicorn app.main:app --host 0.0.0.0 --port $PORT",
            },
        },
    }
    kinds = [k for k, _ in events]
    assert kinds[0] == "deploy.repo" and kinds[-1] == "deploy.live"
    assert [m for k, m in events if k == "deploy.render_status"] == ["build_in_progress", "live"]


def test_every_render_request_carries_the_key_header_only(
    github: FakeGitHub, render: FakeRender
) -> None:
    deploy(github, render)
    for request in render.requests:
        assert request.headers["authorization"] == f"Bearer {KEY}"
        assert KEY not in str(request.url)


# -- redeploys: the same service, exact commits -----------------------------------------------
def test_a_changed_project_deploys_exactly_the_new_commit_to_the_same_service(
    github: FakeGitHub, render: FakeRender
) -> None:
    deploy(github, render)
    render.requests.clear()

    deploy(github, render, files={**FILES, "app/extra.py": b"X = 1\n"})

    assert "POST /v1/services" not in render.writes()  # same service
    new_deploy = list(render.deploys.values())[-1]
    assert new_deploy["commit"] == github.repos["sda-api"]["refs"]["main"]
    assert new_deploy["status"] == "live"


def test_an_unchanged_live_service_is_not_redeployed(
    github: FakeGitHub, render: FakeRender
) -> None:
    deploy(github, render)
    render.requests.clear()
    url, events = deploy(github, render)
    assert url == "https://sda-api.onrender.com"
    assert render.writes() == []
    assert "deploy.unchanged" in [k for k, _ in events]


def test_an_unchanged_project_whose_last_deploy_failed_is_deployed_again(
    github: FakeGitHub, render: FakeRender
) -> None:
    render.outcome = "build_failed"
    with pytest.raises(DeployError):
        deploy(github, render)
    render.outcome = "live"
    deploy(github, render)
    assert list(render.deploys.values())[-1]["commit"] == github.repos["sda-api"]["refs"]["main"]


def test_changed_commands_update_the_service_before_deploying(
    github: FakeGitHub, render: FakeRender
) -> None:
    deploy(github, render)
    flask = Detection(
        kind="python",
        target="render",
        framework="flask",
        reason="",
        build_command="pip install -r requirements.txt && pip install gunicorn",
        start_command="gunicorn app.main:app --bind 0.0.0.0:$PORT",
    )
    deploy(github, render, detection=flask)
    service = next(iter(render.services.values()))
    assert service["serviceDetails"]["envSpecificDetails"]["startCommand"].startswith("gunicorn")
    writes = render.writes()
    assert writes.index(f"PATCH /v1/services/{service['id']}") < len(writes) - 1  # then a deploy


# -- never touching what isn't ours ----------------------------------------------------------
def test_a_same_named_service_on_another_repo_is_refused_before_anything_is_written(
    github: FakeGitHub, render: FakeRender
) -> None:
    render.add_service("sda-api", "https://github.com/someone-else/their-api")
    with pytest.raises(DeployError, match="not this project's repo"):
        deploy(github, render)
    assert github.writes() == [] and render.writes() == []


def test_several_workspaces_need_an_explicit_choice(github: FakeGitHub) -> None:
    render = FakeRender(owners=2)
    with pytest.raises(DeployError, match="2 workspaces .*team 0 \\(tea-0\\).*RENDER_OWNER_ID"):
        deploy(github, render)
    assert github.writes() == [] and render.writes() == []

    render.requests.clear()
    deploy(github, render, owner_id="tea-1")
    assert next(iter(render.services.values()))["created_with"]["ownerId"] == "tea-1"
    assert not any(r.url.path == "/v1/owners" for r in render.requests)  # no lookup needed


# -- failures are explained -------------------------------------------------------------------
def test_render_without_access_to_the_repo_explains_the_github_app(github: FakeGitHub) -> None:
    render = FakeRender(reachable_repos=set())
    with pytest.raises(DeployError, match="GitHub app.*all repositories"):
        deploy(github, render)


def test_a_failed_build_points_at_the_service_logs(github: FakeGitHub) -> None:
    render = FakeRender(outcome="build_failed")
    with pytest.raises(DeployError, match="ended 'build_failed'.*dashboard.render.com/web/srv-1"):
        deploy(github, render)


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(401, json={"message": "unauthorized"}), "rejected the API key"),
        (httpx.Response(402, json={"message": "payment required"}), "payment details"),
        (
            httpx.Response(429, json={"message": "slow down"}, headers={"ratelimit-reset": "30"}),
            "resets in 30 s",
        ),
    ],
)
def test_api_errors_are_explained_without_the_key(
    github: FakeGitHub, render: FakeRender, response: httpx.Response, message: str
) -> None:
    render.fail[("GET", "/v1/owners")] = response
    with pytest.raises(DeployError, match=message) as caught:
        deploy(github, render)
    assert KEY not in str(caught.value) and TOKEN not in str(caught.value)


def test_no_key_and_no_network_are_explained() -> None:
    with pytest.raises(DeployError, match="no Render API key"):
        RenderClient("")

    def down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    with pytest.raises(DeployError, match="could not reach Render: ConnectTimeout"):
        RenderClient(KEY, transport=httpx.MockTransport(down)).owner_id()


def test_only_render_targets_are_deployed_here(github: FakeGitHub, render: FakeRender) -> None:
    static = Detection(kind="static", target="github-pages", framework="html", reason="")
    with pytest.raises(DeployError, match="not deployed to Render"):
        deploy(github, render, detection=static)
    assert github.requests == [] and render.requests == []


def test_a_deploy_that_never_goes_live_times_out(render: FakeRender) -> None:
    render.add_service("sda-api", "https://github.com/octo/sda-api", status="build_in_progress")
    client = RenderClient(KEY, transport=httpx.MockTransport(render), sleep=lambda _s: None)
    service = client.find_service("tea-0", "sda-api")
    deploy_id = next(iter(render.deploys))
    render.deploys[deploy_id]["polls"] = -(10**9)  # stays in progress
    with pytest.raises(DeployError, match="did not go live within 0 s"):
        client.wait_for_deploy(service, deploy_id, lambda *_: None, timeout=0.05, every=0)

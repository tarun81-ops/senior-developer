"""Phase 5, P5.3: the GitHub client against an in-memory fake GitHub (D38). Offline."""

from __future__ import annotations

import base64
import hashlib
import json
from urllib.parse import parse_qs

import httpx
import pytest

from backend.core.deploy.detect import Detection
from backend.core.deploy.github import (
    MARKER,
    WORKFLOW_PATH,
    DeployError,
    GitHubClient,
    deploy_static,
    pages_workflow,
)

TOKEN = "ghp_" + "t" * 36  # fake, built at runtime
HTML = Detection(kind="static", target="github-pages", framework="html", reason="", publish_dir=".")
VITE = Detection(
    kind="static",
    target="github-pages",
    framework="vite",
    reason="",
    build_command="(npm ci || npm install) && npm run build -- --base=./",
    publish_dir="dist",
)


def _sha(*parts: object) -> str:
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


class FakeGitHub:
    """Just enough of GitHub's REST API, with real git-like state."""

    def __init__(self, *, login: str = "octo", run_outcome: str = "success") -> None:
        self.login = login
        self.run_outcome = run_outcome
        self.repos: dict[str, dict] = {}
        self.requests: list[httpx.Request] = []
        self.fail: dict[tuple[str, str], httpx.Response] = {}

    # state helpers --------------------------------------------------------------
    def add_repo(self, name: str, *, private: bool, description: str = MARKER) -> dict:
        repo = {
            "data": {
                "name": name,
                "private": private,
                "description": description,
                "default_branch": "main",
                "owner": {"login": self.login},
                "html_url": f"https://github.com/{self.login}/{name}",
            },
            "blobs": {},
            "trees": {},
            "commits": {},
            "refs": {},
            "pages": None,
            "runs": [],
        }
        self.repos[name] = repo
        blob = self._blob(name, b"# readme\n")
        tree = self._tree(name, [{"path": "README.md", "sha": blob}])
        commit = self._commit(name, tree, [], "Initial commit")
        repo["refs"]["main"] = commit
        return repo

    def _blob(self, repo: str, content: bytes) -> str:
        sha = hashlib.sha1(content).hexdigest()
        self.repos[repo]["blobs"][sha] = content
        return sha

    def _tree(self, repo: str, entries: list[dict]) -> str:
        norm = sorted((e["path"], e["sha"]) for e in entries)
        sha = _sha("tree", norm)
        self.repos[repo]["trees"][sha] = dict(norm)
        return sha

    def _commit(self, repo: str, tree: str, parents: list[str], message: str) -> str:
        sha = _sha("commit", tree, parents, message)
        self.repos[repo]["commits"][sha] = {"tree": tree, "parents": parents, "message": message}
        return sha

    def files(self, repo: str) -> dict[str, bytes]:
        state = self.repos[repo]
        tree = state["trees"][state["commits"][state["refs"]["main"]]["tree"]]
        return {path: state["blobs"][sha] for path, sha in tree.items()}

    def writes(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests if r.method != "GET"]

    # the transport ----------------------------------------------------------------
    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        if key in self.fail:
            return self.fail[key]
        body = json.loads(request.content) if request.content else {}
        parts = request.url.path.strip("/").split("/")
        if parts == ["user"]:
            return httpx.Response(200, json={"login": self.login})
        if parts == ["user", "repos"] and request.method == "POST":
            repo = self.add_repo(
                body["name"], private=body["private"], description=body["description"]
            )
            return httpx.Response(201, json=repo["data"])
        if parts[0] != "repos" or len(parts) < 3 or parts[2] not in self.repos:
            return httpx.Response(404, json={"message": "Not Found"})
        name, state, rest = parts[2], self.repos[parts[2]], parts[3:]
        if not rest:
            return httpx.Response(200, json=state["data"])
        if rest[:3] == ["git", "ref", "heads"]:
            return httpx.Response(200, json={"object": {"sha": state["refs"][rest[3]]}})
        if rest[:2] == ["git", "commits"] and request.method == "GET":
            return httpx.Response(200, json={"tree": {"sha": state["commits"][rest[2]]["tree"]}})
        if rest == ["git", "blobs"]:
            return httpx.Response(
                201, json={"sha": self._blob(name, base64.b64decode(body["content"]))}
            )
        if rest == ["git", "trees"]:
            assert "base_tree" not in body, "a snapshot must be a full tree"
            return httpx.Response(201, json={"sha": self._tree(name, body["tree"])})
        if rest == ["git", "commits"]:
            return httpx.Response(
                201,
                json={"sha": self._commit(name, body["tree"], body["parents"], body["message"])},
            )
        if rest[:3] == ["git", "refs", "heads"] and request.method == "PATCH":
            branch = rest[3]
            assert state["commits"][body["sha"]]["parents"] == [state["refs"][branch]], (
                "not a fast-forward"
            )
            state["refs"][branch] = body["sha"]
            if WORKFLOW_PATH in self.files(name):
                state["runs"].insert(
                    0, {"head_sha": body["sha"], "path": WORKFLOW_PATH, "polls": 0}
                )
            return httpx.Response(200, json={})
        if rest == ["pages"]:
            if request.method == "GET":
                if state["pages"] is None:
                    return httpx.Response(404, json={"message": "Not Found"})
                return httpx.Response(200, json=state["pages"])
            state["pages"] = {
                "build_type": body["build_type"],
                "html_url": f"https://{self.login}.github.io/{name}/",
            }
            return httpx.Response(201 if request.method == "POST" else 204, json=state["pages"])
        if rest == ["actions", "runs"]:
            sha = parse_qs(request.url.query.decode())["head_sha"][0]
            runs = []
            for run in state["runs"]:
                if run["head_sha"] != sha:
                    continue
                run["polls"] += 1  # queued -> in progress -> completed as it is polled
                done = run["polls"] >= 3
                runs.append(
                    {
                        "path": run["path"],
                        "status": "completed"
                        if done
                        else ("in_progress" if run["polls"] == 2 else "queued"),
                        "conclusion": self.run_outcome if done else None,
                        "html_url": f"https://github.com/{self.login}/{name}/actions/runs/1",
                    }
                )
            return httpx.Response(200, json={"workflow_runs": runs})
        return httpx.Response(404, json={"message": "Not Found"})


@pytest.fixture
def github() -> FakeGitHub:
    return FakeGitHub()


def client_for(fake: FakeGitHub) -> GitHubClient:
    return GitHubClient(TOKEN, transport=httpx.MockTransport(fake), sleep=lambda _s: None)


def deploy(
    fake: FakeGitHub, files: dict[str, bytes], detection: Detection = HTML
) -> tuple[str, list]:
    events: list[tuple[str, str]] = []
    url = deploy_static(
        client_for(fake),
        name="sda-calc",
        detection=detection,
        files=sorted(files.items()),
        emit=lambda k, m: events.append((k, m)),
    )
    return url, events


SITE = {"index.html": b"<h1>calc</h1>", "app.js": b"console.log(1)"}


# -- the first deploy ------------------------------------------------------------------
def test_first_deploy_creates_a_public_repo_enables_pages_and_publishes(github: FakeGitHub) -> None:
    url, events = deploy(github, SITE)

    assert url == "https://octo.github.io/sda-calc/"
    repo = github.repos["sda-calc"]["data"]
    assert repo["private"] is False and repo["description"] == MARKER
    assert github.repos["sda-calc"]["pages"]["build_type"] == "workflow"
    # the branch holds exactly the project plus the workflow: the README from
    # the repo's initial commit is gone (a snapshot, not a merge)
    assert set(github.files("sda-calc")) == {"index.html", "app.js", WORKFLOW_PATH}
    assert github.files("sda-calc")["index.html"] == b"<h1>calc</h1>"
    assert [k for k, _ in events] == [
        "deploy.repo",
        "deploy.pages",
        "deploy.pushed",
        "deploy.building",
        "deploy.live",
    ]
    # Pages is enabled before the push, so the push's workflow run can deploy
    writes = github.writes()
    assert writes.index("POST /repos/octo/sda-calc/pages") < writes.index(
        "PATCH /repos/octo/sda-calc/git/refs/heads/main"
    )


def test_every_request_carries_the_token_header_and_pinned_version(github: FakeGitHub) -> None:
    deploy(github, SITE)
    for request in github.requests:
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert request.headers["x-github-api-version"] == "2022-11-28"
        assert TOKEN not in str(request.url)


# -- redeploys: the same stable repo -----------------------------------------------------
def test_redeploying_the_same_files_changes_nothing(github: FakeGitHub) -> None:
    deploy(github, SITE)
    head = github.repos["sda-calc"]["refs"]["main"]
    runs = len(github.repos["sda-calc"]["runs"])
    github.requests.clear()

    url, events = deploy(github, SITE)

    assert url == "https://octo.github.io/sda-calc/"
    assert github.repos["sda-calc"]["refs"]["main"] == head  # no new commit
    assert len(github.repos["sda-calc"]["runs"]) == runs  # no new build
    assert "deploy.unchanged" in [k for k, _ in events]
    assert "POST /user/repos" not in github.writes()


def test_redeploying_changed_files_updates_the_same_repo_and_drops_deleted_ones(
    github: FakeGitHub,
) -> None:
    deploy(github, SITE)
    first = github.repos["sda-calc"]["refs"]["main"]
    github.requests.clear()

    deploy(github, {"index.html": b"<h1>calc v2</h1>"})

    assert "POST /user/repos" not in github.writes()
    state = github.repos["sda-calc"]
    assert state["commits"][state["refs"]["main"]]["parents"] == [first]  # fast-forward
    assert set(github.files("sda-calc")) == {"index.html", WORKFLOW_PATH}  # app.js is gone
    assert github.files("sda-calc")["index.html"] == b"<h1>calc v2</h1>"


# -- never touching what isn't ours -------------------------------------------------------
def test_a_same_named_repo_this_app_did_not_create_is_never_touched(github: FakeGitHub) -> None:
    github.add_repo("sda-calc", private=False, description="my own project")
    with pytest.raises(DeployError, match="not created by this app"):
        deploy(github, SITE)
    assert github.writes() == []  # nothing was written anywhere


def test_a_static_repo_that_became_private_is_not_flipped_back(github: FakeGitHub) -> None:
    github.add_repo("sda-calc", private=True)
    with pytest.raises(DeployError, match="is not public"):
        deploy(github, SITE)
    assert github.repos["sda-calc"]["data"]["private"] is True
    assert github.writes() == []


def test_pages_left_on_branch_builds_is_switched_to_the_workflow(github: FakeGitHub) -> None:
    repo = github.add_repo("sda-calc", private=False)
    repo["pages"] = {"build_type": "legacy", "html_url": "https://octo.github.io/sda-calc/"}
    deploy(github, SITE)
    assert "PUT /repos/octo/sda-calc/pages" in github.writes()
    assert repo["pages"]["build_type"] == "workflow"


# -- the workflow ---------------------------------------------------------------------------
def test_the_vite_workflow_builds_with_current_actions_and_publishes_dist(
    github: FakeGitHub,
) -> None:
    deploy(github, {"package.json": b"{}", "index.html": b""}, detection=VITE)
    workflow = github.files("sda-calc")[WORKFLOW_PATH].decode()
    for action in (
        "actions/checkout@v7",
        "actions/setup-node@v7",
        "actions/configure-pages@v6",
        "actions/upload-pages-artifact@v5",
        "actions/deploy-pages@v5",
    ):
        assert action in workflow, action
    assert "run: (npm ci || npm install) && npm run build -- --base=./" in workflow
    assert "path: dist" in workflow
    assert "branches: [main]" in workflow


def test_the_plain_site_workflow_has_no_build_step() -> None:
    workflow = pages_workflow(HTML, "main")
    assert "setup-node" not in workflow and "run:" not in workflow
    assert "path: ." in workflow


def test_a_workflow_file_in_the_project_cannot_replace_ours(github: FakeGitHub) -> None:
    deploy(github, {**SITE, WORKFLOW_PATH: b"name: something else\n"})
    assert github.files("sda-calc")[WORKFLOW_PATH].startswith(b"# " + MARKER.encode())


# -- failures are explained, never leak the token --------------------------------------------
def test_a_failed_pages_build_says_so_with_its_log(github: FakeGitHub) -> None:
    github.run_outcome = "failure"
    with pytest.raises(DeployError, match="ended 'failure'.*actions/runs/1"):
        deploy(github, SITE)


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(401, json={"message": "Bad credentials"}), "rejected the token"),
        (
            httpx.Response(
                403,
                json={"message": "API rate limit exceeded"},
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1790000000"},
            ),
            "rate limit",
        ),
        (
            httpx.Response(
                403, json={"message": "Resource not accessible by personal access token"}
            ),
            "lacks a permission",
        ),
    ],
)
def test_api_errors_are_explained_without_the_token(
    github: FakeGitHub, response: httpx.Response, message: str
) -> None:
    github.fail[("GET", "/user")] = response
    with pytest.raises(DeployError, match=message) as caught:
        deploy(github, SITE)
    assert TOKEN not in str(caught.value)


def test_a_network_failure_is_explained() -> None:
    def down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    client = GitHubClient(TOKEN, transport=httpx.MockTransport(down))
    with pytest.raises(DeployError, match="could not reach GitHub: ConnectError"):
        client.login()


def test_no_token_is_refused_before_any_request() -> None:
    with pytest.raises(DeployError, match="no GitHub token"):
        GitHubClient("")


def test_a_build_that_never_finishes_times_out(github: FakeGitHub) -> None:
    client = client_for(github)
    repo, _ = client.ensure_repo("octo", "sda-calc", private=False)
    commit = client.commit_snapshot(repo, [("index.html", b"x"), (WORKFLOW_PATH, b"x")], "m")
    github.repos["sda-calc"]["runs"][0]["polls"] = -(10**9)  # stays queued
    with pytest.raises(DeployError, match="did not finish within 0 s"):
        client.wait_for_pages_run(repo, commit, timeout=0.05, every=0)


def test_only_github_pages_targets_are_deployed_here(github: FakeGitHub) -> None:
    backend = Detection(kind="python", target="render", framework="fastapi", reason="")
    with pytest.raises(DeployError, match="not deployed to GitHub Pages"):
        deploy(github, SITE, detection=backend)
    assert github.requests == []

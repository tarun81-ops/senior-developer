"""GitHub: one stable repo per workspace folder, and GitHub Pages for static sites (D38).

REST only, over ``httpx``, like the provider clients: git does not need to be
installed. A deploy is:

1. **ensure the repo** ``sda-<folder>``. It is created on the first deploy
   (public for a static site, private for a backend, D36). An existing repo
   is used only if this app created it (its description carries
   :data:`MARKER`), so a repo of yours that happens to share the name is
   never overwritten;
2. for a static site, **enable Pages** with ``build_type: workflow`` before
   pushing, so the push's workflow run can deploy;
3. **commit a snapshot**: every published file becomes a blob, one full tree
   (files deleted from the project disappear from the repo too), one commit,
   and a fast-forward of the default branch. An unchanged project makes no
   commit, so a second deploy of the same files is a no-op;
4. for a static site, **wait for the Pages workflow** on that commit.

The token is sent only in the ``Authorization`` header, and no error message
ever contains it.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from backend.core.deploy.detect import Detection

API = "https://api.github.com"
#: Pinned: its behaviour is known for every endpoint used here. 2026-03-10
#: also exists; moving needs a read of its changes first (D38).
API_VERSION = "2022-11-28"
#: In the description of every repo this app creates; also its ownership check.
MARKER = "Deployed by Senior Developer Agents"
WORKFLOW_PATH = ".github/workflows/sda-pages.yml"

Emit = Callable[[str, str], None]  # (kind, message)


class DeployError(RuntimeError):
    """A deploy step failed. The message is safe to show and log (no token)."""


@dataclass(frozen=True)
class Repo:
    owner: str
    name: str
    private: bool
    default_branch: str
    html_url: str


class GitHubClient:
    def __init__(
        self,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not token:
            raise DeployError("no GitHub token is set (Settings → API keys → GITHUB_TOKEN)")
        self._http = httpx.Client(
            base_url=API,
            transport=transport,
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "senior-developer-agents",
            },
        )
        self._sleep = sleep

    def close(self) -> None:
        self._http.close()

    # -- requests ------------------------------------------------------------
    def _call(
        self, method: str, path: str, *, ok: tuple[int, ...] = (200, 201), **kw
    ) -> httpx.Response:
        try:
            response = self._http.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise DeployError(f"could not reach GitHub: {type(exc).__name__}") from None
        if response.status_code in ok:
            return response
        raise DeployError(_explain(response, f"{method} {path}"))

    # -- account and repo -------------------------------------------------------
    def login(self) -> str:
        return self._call("GET", "/user").json()["login"]

    def ensure_repo(self, owner: str, name: str, *, private: bool) -> tuple[Repo, bool]:
        """``(repo, created)``. Refuses a same-named repo this app did not create."""
        found = self._call("GET", f"/repos/{owner}/{name}", ok=(200, 404))
        if found.status_code == 200:
            data = found.json()
            if MARKER not in (data.get("description") or ""):
                raise DeployError(
                    f"a repository named {owner}/{name} already exists and was not created by "
                    "this app, so it will not be overwritten. Rename or delete it to deploy here."
                )
            if bool(data["private"]) != private:
                want = "private" if private else "public"
                raise DeployError(
                    f"{owner}/{name} exists but is not {want}, which this kind of project needs "
                    "(D36). Its visibility is not changed automatically; change it on GitHub."
                )
            return _repo(data), False
        data = self._call(
            "POST",
            "/user/repos",
            json={
                "name": name,
                "private": private,
                "description": MARKER,
                # an initial commit gives the repo a default branch to build on
                "auto_init": True,
                "has_issues": False,
                "has_wiki": False,
                "has_projects": False,
            },
        ).json()
        return _repo(data), True

    # -- commits ----------------------------------------------------------------
    def commit_snapshot(
        self, repo: Repo, files: list[tuple[str, bytes]], message: str
    ) -> str | None:
        """Make the default branch hold exactly ``files``. Returns the new commit sha,
        or ``None`` when the branch already held exactly these files."""
        base = f"/repos/{repo.owner}/{repo.name}/git"
        head = self._call("GET", f"{base}/ref/heads/{repo.default_branch}").json()["object"]["sha"]
        base_tree = self._call("GET", f"{base}/commits/{head}").json()["tree"]["sha"]
        entries = []
        for path, content in files:
            blob = self._call(
                "POST",
                f"{base}/blobs",
                json={"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"},
            ).json()["sha"]
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob})
        # no base_tree: the new tree is the whole project, so deletions carry over
        tree = self._call("POST", f"{base}/trees", json={"tree": entries}).json()["sha"]
        if tree == base_tree:
            return None
        commit = self._call(
            "POST", f"{base}/commits", json={"message": message, "tree": tree, "parents": [head]}
        ).json()["sha"]
        self._call(
            "PATCH",
            f"{base}/refs/heads/{repo.default_branch}",
            json={"sha": commit, "force": False},
        )
        return commit

    # -- Pages ---------------------------------------------------------------------
    def ensure_pages(self, repo: Repo) -> str:
        """Pages built by our workflow. Returns the site URL."""
        path = f"/repos/{repo.owner}/{repo.name}/pages"
        found = self._call("GET", path, ok=(200, 404))
        if found.status_code == 404:
            data = self._call("POST", path, json={"build_type": "workflow"}).json()
        else:
            data = found.json()
            if data.get("build_type") != "workflow":
                self._call("PUT", path, json={"build_type": "workflow"}, ok=(204,))
        return data.get("html_url") or f"https://{repo.owner.lower()}.github.io/{repo.name}/"

    def wait_for_pages_run(
        self, repo: Repo, commit: str, *, timeout: float = 600, every: float = 5
    ) -> None:
        """Wait for the Pages workflow run on ``commit`` to succeed, or explain why not."""
        runs = f"/repos/{repo.owner}/{repo.name}/actions/runs"
        deadline = time.monotonic() + timeout
        while True:
            listed = self._call("GET", runs, params={"head_sha": commit, "per_page": 10}).json()
            ours = [r for r in listed.get("workflow_runs", []) if r.get("path") == WORKFLOW_PATH]
            if ours and ours[0].get("status") == "completed":
                if ours[0].get("conclusion") == "success":
                    return
                raise DeployError(
                    f"the Pages build ended '{ours[0].get('conclusion')}'; "
                    f"see its log at {ours[0].get('html_url')}"
                )
            if time.monotonic() >= deadline:
                raise DeployError(f"the Pages build did not finish within {int(timeout)} s")
            self._sleep(every)


# -- the static-site deploy ---------------------------------------------------------
def pages_workflow(detection: Detection, branch: str) -> str:
    """The workflow that builds (for Vite) and publishes the site."""
    build = ""
    if detection.framework == "vite":
        build = (
            "      - uses: actions/setup-node@v7\n"
            "        with:\n"
            "          node-version: lts/*\n"
            f"      - run: {detection.build_command}\n"
        )
    return (
        f"# {MARKER}. Rewritten on every deploy; edit the project, not this file.\n"
        "name: Deploy to GitHub Pages\n"
        "on:\n"
        "  push:\n"
        f"    branches: [{branch}]\n"
        "  workflow_dispatch:\n"
        "permissions:\n"
        "  contents: read\n"
        "  pages: write\n"
        "  id-token: write\n"
        "concurrency:\n"
        "  group: pages\n"
        "  cancel-in-progress: true\n"
        "jobs:\n"
        "  deploy:\n"
        "    runs-on: ubuntu-latest\n"
        "    environment:\n"
        "      name: github-pages\n"
        "      url: ${{ steps.deployment.outputs.page_url }}\n"
        "    steps:\n"
        "      - uses: actions/checkout@v7\n"
        f"{build}"
        "      - uses: actions/configure-pages@v6\n"
        "      - uses: actions/upload-pages-artifact@v5\n"
        "        with:\n"
        f"          path: {detection.publish_dir}\n"
        "      - id: deployment\n"
        "        uses: actions/deploy-pages@v5\n"
    )


def deploy_static(
    client: GitHubClient,
    *,
    name: str,
    detection: Detection,
    files: list[tuple[str, bytes]],
    emit: Emit,
    wait: bool = True,
) -> str:
    """Publish a static site to ``<login>.github.io/<name>/``. Returns the URL."""
    if detection.target != "github-pages":
        raise DeployError(f"{detection.framework} projects are not deployed to GitHub Pages")
    owner = client.login()
    repo, created = client.ensure_repo(owner, name, private=False)
    emit("deploy.repo", f"{'created' if created else 'using'} {repo.html_url} (public)")
    url = client.ensure_pages(repo)
    emit("deploy.pages", f"GitHub Pages builds from the workflow; site: {url}")
    snapshot = [(p, c) for p, c in files if p != WORKFLOW_PATH]
    snapshot.append((WORKFLOW_PATH, pages_workflow(detection, repo.default_branch).encode("utf-8")))
    commit = client.commit_snapshot(
        repo, sorted(snapshot), f"Deploy {name} from Senior Developer Agents"
    )
    if commit is None:
        emit("deploy.unchanged", "the repo already holds exactly these files; nothing to publish")
        return url
    emit("deploy.pushed", f"committed {len(snapshot)} files as {commit[:7]}")
    if wait:
        emit("deploy.building", "waiting for the Pages workflow to build and publish")
        client.wait_for_pages_run(repo, commit)
    emit("deploy.live", url)
    return url


# -- helpers -------------------------------------------------------------------------
def _repo(data: dict) -> Repo:
    return Repo(
        owner=data["owner"]["login"],
        name=data["name"],
        private=bool(data["private"]),
        default_branch=data.get("default_branch") or "main",
        html_url=data["html_url"],
    )


def _explain(response: httpx.Response, what: str) -> str:
    """A safe, specific message for a failed call (never the token)."""
    try:
        detail = response.json().get("message", "")
    except ValueError:
        detail = ""
    status = response.status_code
    if status == 401:
        return "GitHub rejected the token (401). Check GITHUB_TOKEN in Settings."
    if status == 403 and response.headers.get("x-ratelimit-remaining") == "0":
        reset = response.headers.get("x-ratelimit-reset", "")
        return f"GitHub's rate limit is used up; it resets at epoch {reset}."
    lowered = detail.lower()
    if status in (403, 404) and ("permission" in lowered or "resource not accessible" in lowered):
        return (
            f"the GitHub token lacks a permission for {what}: {detail}. It needs Administration, "
            "Contents, Pages and Workflows (read and write) and Actions (read)."
        )
    return f"GitHub answered {status} to {what}" + (f": {detail}" if detail else "")

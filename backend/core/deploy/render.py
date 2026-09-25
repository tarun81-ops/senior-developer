"""Render: one always-on web service per workspace folder, for Python backends (D39).

Render deploys only from a repository (D11 amendment), so a backend deploy is:

1. the private GitHub repo ``sda-<folder>`` gets a snapshot commit (D36, D38);
2. the Render web service ``sda-<folder>`` is found, or created from that repo
   (Python runtime, free plan, build/start commands from detection,
   ``autoDeploy: no``);
3. **exactly that commit** is deployed (``commitId``), and the deploy is
   followed until ``live`` or a failure status.

A same-named service that builds from another repo is not ours and is never
touched. Field names and statuses were checked against Render's published
OpenAPI (https://api.render.com/v1) on 2026-09-26, not written from memory.
The API key is sent only in the ``Authorization`` header.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from backend.core.deploy.detect import Detection
from backend.core.deploy.github import DeployError, Emit, GitHubClient

API = "https://api.render.com/v1"
REGION = "oregon"  # Render's default
PLAN = "free"

_IN_PROGRESS = {
    "created",
    "queued",
    "build_in_progress",
    "update_in_progress",
    "pre_deploy_in_progress",
}
_FAILED = {"build_failed", "update_failed", "pre_deploy_failed", "canceled", "deactivated"}


@dataclass(frozen=True)
class Service:
    id: str
    name: str
    repo: str
    url: str
    dashboard_url: str
    build_command: str
    start_command: str


class RenderClient:
    def __init__(
        self,
        api_key: str,
        *,
        owner_id: str = "",
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise DeployError("no Render API key is set (Settings → API keys → RENDER_API_KEY)")
        self._owner_id = owner_id
        self._http = httpx.Client(
            base_url=API,
            transport=transport,
            timeout=30.0,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )
        self._sleep = sleep

    def close(self) -> None:
        self._http.close()

    def _call(
        self, method: str, path: str, *, ok: tuple[int, ...] = (200, 201, 202), **kw
    ) -> httpx.Response:
        try:
            response = self._http.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise DeployError(f"could not reach Render: {type(exc).__name__}") from None
        if response.status_code in ok:
            return response
        raise DeployError(_explain(response, f"{method} {path}"))

    # -- workspace ----------------------------------------------------------------
    def owner_id(self) -> str:
        """The workspace to deploy into: RENDER_OWNER_ID, or the only one there is."""
        if self._owner_id:
            return self._owner_id
        owners = [
            row["owner"] for row in self._call("GET", "/owners", params={"limit": 100}).json()
        ]
        if len(owners) == 1:
            return owners[0]["id"]
        names = ", ".join(f"{o['name']} ({o['id']})" for o in owners) or "none"
        raise DeployError(
            f"this Render key can reach {len(owners)} workspaces ({names}); "
            "set RENDER_OWNER_ID in Settings to choose one"
        )

    # -- services -------------------------------------------------------------------
    def find_service(self, owner_id: str, name: str) -> Service | None:
        rows = self._call(
            "GET", "/services", params={"name": name, "ownerId": owner_id, "limit": 20}
        ).json()
        exact = [row["service"] for row in rows if row["service"]["name"] == name]
        return _service(exact[0]) if exact else None

    def create_service(
        self, owner_id: str, name: str, repo_url: str, branch: str, detection: Detection
    ) -> tuple[Service, str | None]:
        """``(service, first deploy id)``. Render starts the first deploy itself."""
        data = self._call(
            "POST",
            "/services",
            json={
                "type": "web_service",
                "name": name,
                "ownerId": owner_id,
                "repo": repo_url,
                "branch": branch,
                # only this app starts deploys, each for an exact commit
                "autoDeploy": "no",
                "serviceDetails": {
                    "runtime": "python",
                    "plan": PLAN,
                    "region": REGION,
                    "envSpecificDetails": {
                        "buildCommand": detection.build_command,
                        "startCommand": detection.start_command,
                    },
                },
            },
        ).json()
        return _service(data["service"]), data.get("deployId")

    def update_commands(self, service: Service, detection: Detection) -> None:
        self._call(
            "PATCH",
            f"/services/{service.id}",
            json={
                "serviceDetails": {
                    "envSpecificDetails": {
                        "buildCommand": detection.build_command,
                        "startCommand": detection.start_command,
                    }
                }
            },
        )

    # -- deploys ----------------------------------------------------------------------
    def latest_deploy_status(self, service: Service) -> str | None:
        rows = self._call("GET", f"/services/{service.id}/deploys", params={"limit": 1}).json()
        return rows[0]["deploy"]["status"] if rows else None

    def deploy_commit(self, service: Service, commit: str) -> str:
        data = self._call(
            "POST",
            f"/services/{service.id}/deploys",
            json={"commitId": commit, "clearCache": "do_not_clear"},
        ).json()
        return data["id"]

    def wait_for_deploy(
        self,
        service: Service,
        deploy_id: str,
        emit: Emit,
        *,
        timeout: float = 900,
        every: float = 5,
    ) -> None:
        deadline = time.monotonic() + timeout
        last = None
        while True:
            status = self._call("GET", f"/services/{service.id}/deploys/{deploy_id}").json()[
                "status"
            ]
            if status != last:
                emit("deploy.render_status", status)
                last = status
            if status == "live":
                return
            if status in _FAILED:
                raise DeployError(
                    f"the Render deploy ended '{status}'; see its logs at {service.dashboard_url}"
                )
            if status not in _IN_PROGRESS:
                raise DeployError(f"Render reported an unknown deploy status '{status}'")
            if time.monotonic() >= deadline:
                raise DeployError(f"the Render deploy did not go live within {int(timeout)} s")
            self._sleep(every)


def deploy_backend(
    github: GitHubClient,
    render: RenderClient,
    *,
    name: str,
    detection: Detection,
    files: list[tuple[str, bytes]],
    emit: Emit,
) -> str:
    """Publish a Python backend to an always-on Render service. Returns its URL."""
    if detection.target != "render":
        raise DeployError(f"{detection.framework} projects are not deployed to Render")
    # Every check that can refuse runs before anything is written anywhere.
    owner_id = render.owner_id()
    existing = render.find_service(owner_id, name)
    login = github.login()
    expected_repo = f"https://github.com/{login}/{name}"
    if existing is not None and existing.repo.rstrip("/").lower() != expected_repo.lower():
        raise DeployError(
            f"a Render service named {name} already exists and deploys "
            f"{existing.repo or 'no repo'}, not this project's repo, so it will not be "
            "touched. Rename or delete it to deploy here."
        )

    repo, created = github.ensure_repo(login, name, private=True)
    emit("deploy.repo", f"{'created' if created else 'using'} {repo.html_url} (private)")
    commit = github.commit_snapshot(
        repo, sorted(files), f"Deploy {name} from Senior Developer Agents"
    )
    emit(
        "deploy.pushed",
        f"committed {len(files)} files as {commit[:7]}" if commit else "no file changes",
    )

    if existing is None:
        service, deploy_id = render.create_service(
            owner_id, name, repo.html_url, repo.default_branch, detection
        )
        emit("deploy.service", f"created Render service {name} ({service.dashboard_url})")
        if deploy_id is None:  # Render normally starts one; if not, start it
            deploy_id = render.deploy_commit(service, commit or github.head_commit(repo))
    else:
        service = existing
        if (service.build_command, service.start_command) != (
            detection.build_command,
            detection.start_command,
        ):
            render.update_commands(service, detection)
            emit("deploy.service", "updated the service's build and start commands")
        elif commit is None and render.latest_deploy_status(service) == "live":
            emit("deploy.unchanged", "the service is live and already runs exactly these files")
            emit("deploy.live", service.url)
            return service.url
        deploy_id = render.deploy_commit(service, commit or github.head_commit(repo))
    emit("deploy.building", "waiting for Render to build and start the service")
    render.wait_for_deploy(service, deploy_id, emit)
    emit("deploy.live", service.url)
    return service.url


# -- helpers ---------------------------------------------------------------------------
def _service(data: dict) -> Service:
    details = data.get("serviceDetails") or {}
    env = details.get("envSpecificDetails") or {}
    return Service(
        id=data["id"],
        name=data["name"],
        repo=data.get("repo") or "",
        url=details.get("url") or "",
        dashboard_url=data.get("dashboardUrl") or "",
        build_command=env.get("buildCommand") or "",
        start_command=env.get("startCommand") or "",
    )


def _explain(response: httpx.Response, what: str) -> str:
    """A safe, specific message for a failed call (never the key)."""
    try:
        detail = response.json().get("message", "")
    except ValueError:
        detail = ""
    status = response.status_code
    if status == 401:
        return "Render rejected the API key (401). Check RENDER_API_KEY in Settings."
    if status == 402:
        return (
            "Render needs payment details on the workspace before it will create "
            "this service (402)."
        )
    if status == 429:
        reset = response.headers.get("ratelimit-reset", "?")
        return f"Render's rate limit is used up; it resets in {reset} s."
    if what.startswith("POST /services") and status in (400, 404) and "repo" in detail.lower():
        return (
            f"Render cannot use the repository: {detail}. Render deploys from GitHub through "
            "its GitHub app: install it for your account with access to all repositories "
            "(new sda-* repos are created on deploy), then deploy again."
        )
    return f"Render answered {status} to {what}" + (f": {detail}" if detail else "")

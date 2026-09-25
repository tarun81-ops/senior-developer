"""Deploys of finished runs: preview, confirm, run in the background (D40).

A deploy is two calls, never one:

1. **preview**: built locally (no network, no token). It covers
   eligibility (D35), detection (D36), the publish set and secret scan (D37),
   missing keys, the target and the expected URL, plus a **fingerprint** of
   exactly those files and that target;
2. **confirm**: sends the fingerprint back. The plan is rebuilt and must match,
   so a project that changed after the preview is refused rather than
   published unseen.

Deploys run on one worker thread (separate from pipeline runs, which never
wait on a deploy), at most one active deploy per project. Each streams its
progress under its own id on the SSE endpoint and ends with one closing event,
``deploy.succeeded`` or ``deploy.failed``, written in the same locked step
that sets its final status (the D23 rule). History is saved like runs (D32).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.api.models import DeployPreviewResponse, DeployRecordResponse
from backend.api.run_manager import RunManager
from backend.core.deploy import Detection, deploy_refusal, detect, repo_name
from backend.core.deploy.github import DeployError, GitHubClient, deploy_static
from backend.core.deploy.publish import PublishSet, build_publish_set
from backend.core.deploy.render import RenderClient, deploy_backend
from backend.core.events import Event

logger = logging.getLogger(__name__)

MAX_TRACKED_DEPLOYS = 200
TERMINAL = frozenset({"succeeded", "failed"})

#: Key variables each target needs. They are read from the environment
#: (loaded from .env, D12) only when a deploy runs, and never stored here.
NEEDED_KEYS = {
    "github-pages": ("GITHUB_TOKEN",),
    "render": ("GITHUB_TOKEN", "RENDER_API_KEY"),
}

#: ``clients(target)`` -> (GitHubClient, RenderClient | None). Tests inject
#: fakes; production builds real ones from the environment.
ClientsFactory = Callable[[str], tuple[GitHubClient, RenderClient | None]]


class DeployRefused(RuntimeError):
    """The deploy may not start (409). The message says why."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _default_clients(target: str) -> tuple[GitHubClient, RenderClient | None]:
    github = GitHubClient(os.environ.get("GITHUB_TOKEN", ""))
    if target != "render":
        return github, None
    render = RenderClient(
        os.environ.get("RENDER_API_KEY", ""), owner_id=os.environ.get("RENDER_OWNER_ID", "")
    )
    return github, render


@dataclass
class DeployRecord:
    deploy_id: str
    run_id: str
    project: str
    target: str
    fingerprint: str
    status: str = "queued"
    url: str | None = None
    error: str | None = None
    created_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    finished_at: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    def to_response(self) -> DeployRecordResponse:
        data = asdict(self)
        data.pop("fingerprint")
        return DeployRecordResponse(**data)


@dataclass
class _Plan:
    run_id: str
    project: str
    project_dir: Path | None
    detection: Detection
    publish: PublishSet
    blockers: list[str]
    missing_keys: list[str]
    fingerprint: str


class DeployManager:
    def __init__(
        self,
        *,
        runs: RunManager,
        event_store: Any,
        clients: ClientsFactory | None = None,
    ) -> None:
        self.runs = runs
        self.event_store = event_store
        self._clients = clients or _default_clients
        self._lock = threading.RLock()
        self._deploys: dict[str, DeployRecord] = {}
        self._order: list[str] = []
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sda-deploy")
        self._load_history()

    # -- the plan (shared by preview and confirm) --------------------------------------
    def _plan(self, run_id: str) -> _Plan:
        state = self.runs.detail(run_id).model_dump()  # RunNotFound -> 404
        folder = self.runs.project_dir(run_id)
        blockers: list[str] = []
        refusal = deploy_refusal(state)
        if refusal:
            blockers.append(refusal)
        if folder is None or not folder.is_dir():
            detection = Detection(kind="unsupported", reason="the run has no project folder")
            publish = PublishSet(problem="the project folder does not exist")
        else:
            detection = detect(folder)
            publish = build_publish_set(folder)
        if not detection.deployable:
            blockers.append(detection.reason)
        if publish.findings:
            found = ", ".join(f"{f.path}:{f.line} ({f.kind})" for f in publish.findings)
            blockers.append(f"possible secrets must be removed first: {found}")
        if publish.problem:
            blockers.append(publish.problem)
        missing = [
            name for name in NEEDED_KEYS.get(detection.target or "", ()) if not os.environ.get(name)
        ]
        if missing:
            blockers.append(f"set {', '.join(missing)} in Settings first")
        return _Plan(
            run_id=run_id,
            project=state["project"],
            project_dir=folder,
            detection=detection,
            publish=publish,
            blockers=blockers,
            missing_keys=missing,
            fingerprint=_fingerprint(folder, detection, publish),
        )

    def preview(self, run_id: str) -> DeployPreviewResponse:
        plan = self._plan(run_id)
        d = plan.detection
        name = repo_name(plan.project)
        visibility = {"github-pages": "public", "render": "private"}.get(d.target or "")
        url = {
            "github-pages": f"https://<your GitHub username>.github.io/{name}/",
            "render": f"https://{name}.onrender.com (Render adds a suffix if the name is taken)",
        }.get(d.target or "")
        return DeployPreviewResponse(
            run_id=run_id,
            project=plan.project,
            can_deploy=not plan.blockers,
            blockers=plan.blockers,
            target={
                k: getattr(d, k)
                for k in (
                    "kind",
                    "reason",
                    "target",
                    "framework",
                    "build_command",
                    "start_command",
                    "publish_dir",
                )
            },
            repo=name,
            visibility=visibility,
            expected_url=url,
            files=[{"path": p, "size": s} for p, s in plan.publish.files],
            excluded=[{"path": p, "reason": r} for p, r in plan.publish.excluded],
            findings=[asdict(f) for f in plan.publish.findings],
            total_bytes=plan.publish.total_bytes,
            missing_keys=plan.missing_keys,
            fingerprint=plan.fingerprint,
        )

    # -- confirm ---------------------------------------------------------------------------
    def start(self, run_id: str, fingerprint: str) -> DeployRecord:
        plan = self._plan(run_id)
        if plan.blockers:
            raise DeployRefused("; ".join(plan.blockers))
        if not secrets.compare_digest(plan.fingerprint, fingerprint):
            raise DeployRefused(
                "the project changed since the preview; review the new preview before deploying"
            )
        with self._lock:
            busy = [
                d for d in self._deploys.values() if d.project == plan.project and not d.is_terminal
            ]
            if busy:
                raise DeployRefused(
                    f"{plan.project} is already being deployed ({busy[0].deploy_id})"
                )
            record = DeployRecord(
                deploy_id=_new_deploy_id(),
                run_id=run_id,
                project=plan.project,
                target=plan.detection.target or "",
                fingerprint=plan.fingerprint,
            )
            self._deploys[record.deploy_id] = record
            self._order.append(record.deploy_id)
            self._emit(record, "deploy.queued", f"deploying {plan.project} to {record.target}")
        self._executor.submit(self._work, record, plan)
        return record

    def _work(self, record: DeployRecord, plan: _Plan) -> None:
        github = render = None
        try:
            with self._lock:
                record.status = "running"
                record.started_at = utc_now()
                self._emit(record, "deploy.started", f"{len(plan.publish.files)} files")
            files = [(p, (plan.project_dir / p).read_bytes()) for p, _ in plan.publish.files]
            # the files read now must still be the files that were confirmed
            if (
                _fingerprint(plan.project_dir, plan.detection, build_publish_set(plan.project_dir))
                != plan.fingerprint
            ):
                raise DeployError(
                    "the project changed while the deploy was starting; nothing was published"
                )
            github, render = self._clients(record.target)
            emit = lambda kind, message: self._emit(record, kind, message)  # noqa: E731
            name = repo_name(plan.project)
            if record.target == "github-pages":
                url = deploy_static(
                    github, name=name, detection=plan.detection, files=files, emit=emit
                )
            else:
                url = deploy_backend(
                    github, render, name=name, detection=plan.detection, files=files, emit=emit
                )
            self._finish(record, "succeeded", url=url)
        except DeployError as exc:
            self._finish(record, "failed", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - a worker must never die silently
            logger.exception("deploy %s crashed", record.deploy_id)
            self._finish(record, "failed", error=f"internal error: {type(exc).__name__}")
        finally:
            for client in (github, render):
                if client is not None:
                    client.close()

    def _finish(
        self, record: DeployRecord, status: str, *, url: str | None = None, error: str | None = None
    ) -> None:
        with self._lock:
            if record.is_terminal:
                return
            record.status = status
            record.url = url
            record.error = error
            record.finished_at = utc_now()
            # the closing event, in the same locked step as the status (D23)
            self._emit(record, f"deploy.{status}", url or error or status)

    # -- reading -----------------------------------------------------------------------------
    def list_for_run(self, run_id: str) -> list[DeployRecord]:
        with self._lock:
            return [
                self._deploys[d] for d in reversed(self._order) if self._deploys[d].run_id == run_id
            ]

    def is_finished(self, deploy_id: str) -> bool | None:
        with self._lock:
            record = self._deploys.get(deploy_id)
            return None if record is None else record.is_terminal

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    # -- events and history -----------------------------------------------------------------
    def _emit(self, record: DeployRecord, kind: str, message: str) -> None:
        """Progress on the deploy's own stream, then save the snapshot (D32 pattern)."""
        try:
            self.event_store.sink(
                Event(
                    kind=kind,
                    message=message,
                    run_id=record.deploy_id,
                    data={"run_id": record.run_id},
                )
            )
        except Exception:  # noqa: BLE001 - an event write must never fail a deploy
            logger.warning("could not persist %s for %s", kind, record.deploy_id, exc_info=True)
        try:
            self.event_store.save_deploy(
                record.deploy_id, record.run_id, record.created_at, asdict(record)
            )
        except Exception:  # noqa: BLE001
            logger.warning("could not save deploy %s", record.deploy_id, exc_info=True)

    def _load_history(self) -> None:
        try:
            saved = self.event_store.load_deploys(limit=MAX_TRACKED_DEPLOYS)
        except Exception:  # noqa: BLE001 - history must never stop the API starting
            logger.warning("could not load deploy history", exc_info=True)
            return
        with self._lock:
            for data in saved:
                try:
                    record = DeployRecord(**data)
                except TypeError:
                    logger.warning("skipping unreadable saved deploy %r", data.get("deploy_id"))
                    continue
                self._deploys[record.deploy_id] = record
                self._order.append(record.deploy_id)
            for record in self._deploys.values():
                if not record.is_terminal:
                    # its process stopped mid-deploy; the remote side may be
                    # half-updated, so say so rather than guess
                    self._finish(
                        record,
                        "failed",
                        error="Interrupted: the app stopped during this deploy. "
                        "Deploy again to finish it.",
                    )


# -- helpers --------------------------------------------------------------------------------
def _fingerprint(folder: Path | None, detection: Detection, publish: PublishSet) -> str:
    """sha-256 over the target, the build/start commands, and every file's path and bytes."""
    digest = hashlib.sha256()
    digest.update(json.dumps(asdict(detection), sort_keys=True).encode())
    for path, _size in publish.files:
        digest.update(path.encode() + b"\0")
        try:
            digest.update(hashlib.sha256((folder / path).read_bytes()).digest())
        except OSError:
            digest.update(b"<unreadable>")
    return digest.hexdigest()


def _new_deploy_id() -> str:
    return f"deploy-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"

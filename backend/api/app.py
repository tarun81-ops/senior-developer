"""FastAPI application factory (D17, D19, D20).

The app owns four things and nothing else: the per-launch token, the SQLite
event store, the run manager, and the routes. Every behaviour it exposes lives
in ``backend.core`` (pipeline, sandbox, providers) or in the small services
beside it; no HTTP handler builds a pipeline, a runtime or a query.

* one random token per launch on ``app.state.security``;
* the event repository at ``Settings.db_path`` (``data/app.db``), opened on
  startup, with every run's event bus subscribed to it;
* one :class:`RunManager`, which serialises background runs;
* routes under a router that depends on :func:`require_token`, so a new route is
  protected by default.

The bind address is **not** the app's business — :mod:`backend.api.__main__`
hard-codes ``127.0.0.1`` with no flag to change it. The app additionally
refuses non-loopback ``Host`` headers, so even a misconfigured launcher cannot
serve the API to the network.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from backend import __version__
from backend.api import settings_service
from backend.api.deploy_manager import ClientsFactory, DeployManager, DeployRefused
from backend.api.event_store import EventStore
from backend.api.middleware import BodySizeLimitMiddleware
from backend.api.models import (
    ApprovalDecisionRequest,
    ApprovalResponse,
    CancelResponse,
    CreateRunRequest,
    DeployPreviewResponse,
    DeployRecordResponse,
    DeployRequest,
    DeploysListResponse,
    EventResponse,
    EventsPageResponse,
    FileContentResponse,
    HealthResponse,
    KeysUpdateRequest,
    ProjectsListResponse,
    ProjectSummary,
    RunFilesResponse,
    RunsListResponse,
    RunStateResponse,
    SettingsResponse,
    SettingsUpdateRequest,
)
from backend.api.repository import SqliteEventRepository
from backend.api.run_manager import GateNotWaiting, RunManager, RunNotFound, RunOptions
from backend.api.security import ALLOWED_ORIGINS, LaunchSecurity, require_token
from backend.api.stream import StreamOwners, event_frames
from backend.api.workspace_files import list_files, read_file
from backend.core.config import get_settings, load_env
from backend.core.errors import ConfigError
from backend.core.runtime import Runtime
from backend.core.workspace.sandbox import UnsafePath

logger = logging.getLogger(__name__)

#: Replay page size. Large enough to cover a whole small run, small enough to
#: return in one response without a streaming handshake.
DEFAULT_PAGE_SIZE = 500
MAX_PAGE_SIZE = 2000


def _event_response(stored) -> EventResponse:  # noqa: ANN001 - StoredEvent, avoid import cycle
    return EventResponse(**stored.to_dict())


def get_event_store(request: Request) -> EventStore:
    """FastAPI dependency: the process-wide event store."""
    return request.app.state.event_store


def get_run_manager(request: Request) -> RunManager:
    """FastAPI dependency: the process-wide run manager."""
    return request.app.state.run_manager


def create_app(
    *,
    root: Path | str | None = None,
    security: LaunchSecurity | None = None,
    token: str | None = None,
    runtime_factory: Callable[[str], Runtime] | None = None,
    deploy_clients: ClientsFactory | None = None,
) -> FastAPI:
    """Build the app. Tests pass ``root=tmp_path`` to keep all state in tmp."""
    # State is built *eagerly*, not in the lifespan. A test client that never
    # enters the lifespan (plain ``TestClient(app)``) must still have a working
    # manager, and eager construction has no downside: the repository does not
    # open a connection until the first query, and the manager starts no thread
    # until a run is created. The lifespan is left with shutdown only.
    # load_env first so .env wins over the shell (D12).
    load_env(root)
    resolved = get_settings(root)
    event_store = EventStore(SqliteEventRepository(resolved.db_path))
    # Step 2: one manager for the process. ``runtime_factory`` is the seam tests
    # use to hand it an offline mock runtime, so no endpoint test can reach a
    # paid provider.
    manager = RunManager(
        settings=resolved,
        event_store=event_store,
        runtime_factory=runtime_factory,
    )
    # Phase 5: deploys of finished runs (D40). ``deploy_clients`` is the seam
    # tests use to hand it fake GitHub/Render clients.
    deploys = DeployManager(runs=manager, event_store=event_store, clients=deploy_clients)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info("API ready: db=%s", resolved.db_path)
        try:
            yield
        finally:
            # shutdown() cancels every active run, waits for the workers, and
            # marks the leftovers cancelled so the slot is never left held. It
            # blocks on worker threads, so it runs in a threadpool rather than
            # stalling the event loop.
            await run_in_threadpool(manager.shutdown)
            deploys.shutdown()
            event_store.close()


    app = FastAPI(
        title="Senior Developer Agents API",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # A launcher that already minted a token (the Electron shell) passes it in;
    # otherwise one is generated here. Either way exactly one token exists.
    if security is not None:
        app.state.security = security
    elif token is not None:
        app.state.security = LaunchSecurity(token=token)
    else:
        app.state.security = LaunchSecurity()
    app.state.root = Path(root) if root is not None else None
    # A test may inject a factory here; production leaves it None and
    # RunManager builds real runtimes from config/ and .env.
    app.state.runtime_factory = runtime_factory
    app.state.settings = resolved
    app.state.event_store = event_store
    app.state.run_manager = manager
    app.state.deploy_manager = deploys

    # CORS is a browser rule, not authentication: it only tells a *browser*
    # whether to let page JavaScript read the response. The token still guards
    # every request (security.py). The list is the same one the Origin check
    # uses, so the two can never disagree about the desktop UI's ``sda://app``.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(ALLOWED_ORIGINS),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-API-Key"],
        max_age=600,
    )

    # Added last, so it is the outermost middleware: it is the first thing a
    # request meets and it counts real body bytes, which no header check can do.
    app.add_middleware(BodySizeLimitMiddleware)

    api = APIRouter(prefix="/api", dependencies=[Depends(require_token)])

    @api.get("/health", response_model=HealthResponse)
    def health(request: Request) -> HealthResponse:
        # ASGI exposes the bound address as a ``(host, port)`` tuple. A launcher
        # that asked for an ephemeral port (0) needs the real one to point the
        # desktop shell at the API.
        server = request.scope.get("server") or ("", 0)
        return HealthResponse(
            version=__version__,
            api_port=int(server[1] or 0),
        )

    @api.get("/events", response_model=EventsPageResponse)
    async def replay_events(
        run_id: str = Query(..., min_length=1, max_length=64),
        after_seq: int = Query(-1, ge=-1, description="Return events with seq > this"),
        limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
        store: EventStore = Depends(get_event_store),
    ) -> EventsPageResponse:
        """Replay persisted events for one run (the reconnect path for SSE)."""
        if not _valid_run_id(run_id):
            raise HTTPException(status_code=400, detail="Invalid run_id")
        page = store.replay(run_id, after_seq=after_seq, limit=limit)
        last_seq = page[-1].seq if page else after_seq
        return EventsPageResponse(
            run_id=run_id,
            events=[_event_response(stored) for stored in page],
            last_seq=last_seq,
            has_more=store.last_seq(run_id) > last_seq,
        )

    @api.get("/events/stream")
    async def stream_events(
        request: Request,
        run_id: str = Query(..., min_length=1, max_length=64),
        after_seq: int | None = Query(None, ge=-1, description="Send events with seq > this"),
        last_event_id: str | None = Header(None, alias="Last-Event-ID"),
        store: EventStore = Depends(get_event_store),
        manager: RunManager = Depends(get_run_manager),
    ) -> StreamingResponse:
        """Live SSE stream: replay after the cursor, then live, closing when the
        run ends (D19, D23). Read with ``fetch()`` so ``X-API-Key`` is sent."""
        if not _valid_run_id(run_id):
            raise HTTPException(status_code=400, detail="Invalid run_id")
        if after_seq is None:
            # the standard SSE resume header, for non-UI consumers
            after_seq = int(last_event_id) if (last_event_id or "").isdigit() else -1
        # a run id or a deploy id: each has its own stream (D40)
        owners = StreamOwners(manager, request.app.state.deploy_manager)
        if owners.is_finished(run_id) is None and store.last_seq(run_id) < 0:
            raise HTTPException(status_code=404, detail=f"Unknown run '{run_id}'")
        return StreamingResponse(
            event_frames(store, owners, run_id, after_seq=after_seq),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # -- runs (Part A, step 2) ----------------------------------------------
    @api.post("/runs", response_model=RunStateResponse, status_code=202)
    def create_run(
        body: CreateRunRequest,
        manager: RunManager = Depends(get_run_manager),
    ) -> RunStateResponse:
        """Accept a pipeline run into the queue (D20).

        ``202 Accepted``, not ``201``: the run is queued behind whichever run holds
        the single worker, and no agent has answered yet. The body is the queued
        state, which already carries ``queue_position`` and the event cursor the UI
        needs, so the UI can render the backlog without a second request.
        """
        options = RunOptions.from_request(
            body, default_stages=manager.default_stages()
        )
        record = manager.create(
            request=body.request, options=options, project=body.project
        )
        return manager.detail(record.run_id)

    @api.get("/runs", response_model=RunsListResponse)
    def list_runs(
        limit: int = Query(50, ge=1, le=200),
        manager: RunManager = Depends(get_run_manager),
    ) -> RunsListResponse:
        """Runs created by this API process, newest first."""
        records = manager.list(limit=limit)
        ids = {record.run_id for record in records}
        active = manager.active_run_id
        return RunsListResponse(
            runs=[record.to_summary() for record in records],
            total=manager.count(),
            # Only report an active run the page actually contains: the id of
            # a run outside the page would read as a broken reference.
            active_run_id=active if active in ids else None,
        )

    @api.get("/runs/{run_id}", response_model=RunStateResponse)
    def get_run(
        run_id: str,
        manager: RunManager = Depends(get_run_manager),
    ) -> RunStateResponse:
        """The whole run: board, agent outputs, files written, test results."""
        try:
            return manager.detail(run_id)
        except RunNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @api.post("/runs/{run_id}/cancel", response_model=CancelResponse)
    def cancel_run(
        run_id: str,
        manager: RunManager = Depends(get_run_manager),
    ) -> CancelResponse:
        """Request cancellation. A running command's whole tree is killed (D20)."""
        try:
            cancelled, note = manager.cancel(run_id)
        except RunNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return CancelResponse(
            run_id=run_id,
            cancelled=cancelled,
            status=manager.get(run_id).status,
            note=note,
        )

    # -- run files (Part B, B4; D30) -------------------------------------------
    # Read-only, and only inside the run's own project folder.
    @api.get("/runs/{run_id}/files", response_model=RunFilesResponse)
    def list_run_files(
        run_id: str,
        manager: RunManager = Depends(get_run_manager),
    ) -> RunFilesResponse:
        """Files on disk in the run's project folder (empty before it starts)."""
        try:
            record = manager.get(run_id)
        except RunNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        folder = manager.project_dir(run_id)
        files, truncated = list_files(folder) if folder else ([], False)
        return RunFilesResponse(
            run_id=run_id,
            project=record.project,
            files=[{"path": path, "size": size} for path, size in files],
            truncated=truncated,
        )

    @api.get("/runs/{run_id}/files/content", response_model=FileContentResponse)
    def read_run_file(
        run_id: str,
        path: str = Query(..., min_length=1, max_length=512),
        manager: RunManager = Depends(get_run_manager),
    ) -> FileContentResponse:
        """One file's text (capped; binary files are reported, not decoded)."""
        try:
            folder = manager.project_dir(run_id)
        except RunNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if folder is None:
            raise HTTPException(status_code=404, detail="This run has no project folder yet")
        try:
            found = read_file(folder, path)
        except UnsafePath as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (FileNotFoundError, OSError):
            raise HTTPException(
                status_code=404, detail="No such file in this run's project"
            ) from None
        return FileContentResponse(run_id=run_id, **found.__dict__)

    # -- deploys (Phase 5, P5.5; D35-D40) ---------------------------------------
    # Preview is local and read-only; deploy publishes, so, like approve and
    # reject, it re-checks the token at the call site (D17).
    @api.get("/runs/{run_id}/deploy/preview", response_model=DeployPreviewResponse)
    def deploy_preview(run_id: str) -> DeployPreviewResponse:
        """What a deploy of this run would publish, where, and why it can't yet."""
        try:
            return deploys.preview(run_id)
        except RunNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @api.post("/runs/{run_id}/deploy", response_model=DeployRecordResponse, status_code=202)
    def deploy_run(
        run_id: str,
        body: DeployRequest,
        _auth: None = Depends(require_token),
    ) -> DeployRecordResponse:
        """Publish exactly what the preview with this fingerprint showed."""
        try:
            return deploys.start(run_id, body.fingerprint).to_response()
        except RunNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except DeployRefused as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.get("/runs/{run_id}/deploys", response_model=DeploysListResponse)
    def list_deploys(run_id: str) -> DeploysListResponse:
        """This run's deploys, newest first. Progress streams under each deploy_id."""
        try:
            manager.get(run_id)
        except RunNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return DeploysListResponse(
            run_id=run_id, deploys=[d.to_response() for d in deploys.list_for_run(run_id)]
        )

    # -- approval gates (Part A, step 3) -------------------------------------
    # D17: these two routes re-check the token at the call site, on top of the
    # router-level dependency, so the requirement is visible right where the
    # "approve this command" action is implemented.
    @api.post("/runs/{run_id}/approve", response_model=ApprovalResponse)
    def approve_run(
        run_id: str,
        body: ApprovalDecisionRequest | None = None,
        manager: RunManager = Depends(get_run_manager),
        _auth: None = Depends(require_token),
    ) -> ApprovalResponse:
        """Approve the gate the run is waiting at; the worker resumes."""
        note = body.note if body is not None else None
        try:
            state = manager.approve(run_id, note)
        except RunNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except GateNotWaiting as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return ApprovalResponse(
            run_id=run_id,
            gate=state.gate,
            decision="approved",
            status=manager.get(run_id).status,
            note=state.note,
        )

    @api.post("/runs/{run_id}/reject", response_model=ApprovalResponse)
    def reject_run(
        run_id: str,
        body: ApprovalDecisionRequest | None = None,
        manager: RunManager = Depends(get_run_manager),
        _auth: None = Depends(require_token),
    ) -> ApprovalResponse:
        """Reject the gate: the run stops, ending ``failed`` with reason
        ``rejected`` and this note as its error text."""
        note = body.note if body is not None else None
        try:
            state = manager.reject(run_id, note)
        except RunNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except GateNotWaiting as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return ApprovalResponse(
            run_id=run_id,
            gate=state.gate,
            decision="rejected",
            status=manager.get(run_id).status,
            note=state.note,
        )

    # -- settings (Part A, step 4; D22) ---------------------------------------
    # Read on every request and by every new run, never cached: a PUT takes
    # effect for the next run, and a run in progress keeps the registry it
    # started with.
    @api.get("/settings", response_model=SettingsResponse)
    def get_settings_route() -> SettingsResponse:
        try:
            return settings_service.snapshot(resolved)
        except ConfigError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @api.put("/settings", response_model=SettingsResponse)
    def put_settings(body: SettingsUpdateRequest) -> SettingsResponse:
        """Replace the local overrides. Unknown agent/provider/model is a 422."""
        try:
            return settings_service.update_overrides(resolved, body)
        except ConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @api.put("/settings/keys", response_model=SettingsResponse)
    def put_keys(body: KeysUpdateRequest) -> SettingsResponse:
        """Write-only: keys go to ``.env``; the response says only set/missing."""
        try:
            return settings_service.update_keys(resolved, body.keys)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # -- projects ------------------------------------------------------------
    @api.get("/projects", response_model=ProjectsListResponse)
    def list_projects(manager: RunManager = Depends(get_run_manager)) -> ProjectsListResponse:
        """Project folders that exist in the workspace, newest first.

        Only names, file counts and timestamps are returned — never a path
        outside ``workspace/`` and never file contents (D15).
        """
        projects = sorted(
            manager.workspace_projects(),
            key=lambda folder: folder.get("created_at") or "",
            reverse=True,
        )
        return ProjectsListResponse(
            projects=[ProjectSummary(**folder) for folder in projects],
            total=len(projects),
        )

    app.include_router(api)

    @app.exception_handler(404)
    async def not_found(request: Request, _exc) -> JSONResponse:  # noqa: ANN001
        return JSONResponse(
            {"detail": "Not found. Every /api route needs the X-API-Key header."},
            status_code=404,
        )

    return app


def _valid_run_id(run_id: str) -> bool:
    """Run ids are ``YYYYmmdd-HHMMSS-xxxx``; allow only that alphabet."""
    return all(ch.isalnum() or ch in "-_" for ch in run_id) and len(run_id) <= 64

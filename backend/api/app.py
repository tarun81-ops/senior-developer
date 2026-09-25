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

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from backend import __version__
from backend.api.event_store import EventStore
from backend.api.middleware import BodySizeLimitMiddleware
from backend.api.models import (
    ApprovalDecisionRequest,
    ApprovalResponse,
    CancelResponse,
    CreateRunRequest,
    EventResponse,
    EventsPageResponse,
    HealthResponse,
    ProjectSummary,
    ProjectsListResponse,
    RunStateResponse,
    RunsListResponse,
)
from backend.api.repository import SqliteEventRepository
from backend.api.run_manager import GateNotWaiting, RunManager, RunNotFound, RunOptions
from backend.api.security import ALLOWED_ORIGINS, LaunchSecurity, require_token
from backend.core.config import Settings, get_settings, load_env
from backend.core.runtime import Runtime

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

    # CORS is a browser rule, not authentication: it only tells a *browser*
    # whether to let page JavaScript read the response. The token still guards
    # every request (security.py).
    #
    # The app's own ``sda://`` origin is a regular expression (any host under the
    # scheme), which CORSMiddleware cannot express, so the allowlist keeps the
    # two Vite origins and the Origin *check* in security.py covers ``sda://``.
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
            raise HTTPException(status_code=404, detail=str(exc))

    @api.post("/runs/{run_id}/cancel", response_model=CancelResponse)
    def cancel_run(
        run_id: str,
        manager: RunManager = Depends(get_run_manager),
    ) -> CancelResponse:
        """Request cancellation. A running command's whole tree is killed (D20)."""
        try:
            cancelled, note = manager.cancel(run_id)
        except RunNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return CancelResponse(
            run_id=run_id,
            cancelled=cancelled,
            status=manager.get(run_id).status,
            note=note,
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
            raise HTTPException(status_code=404, detail=str(exc))
        except GateNotWaiting as exc:
            raise HTTPException(status_code=409, detail=str(exc))
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
            raise HTTPException(status_code=404, detail=str(exc))
        except GateNotWaiting as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return ApprovalResponse(
            run_id=run_id,
            gate=state.gate,
            decision="rejected",
            status=manager.get(run_id).status,
            note=state.note,
        )

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

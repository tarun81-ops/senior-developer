"""FastAPI application factory (D17).

Part A step 1 builds the foundations only: ``/api/health`` and ``/api/events``.
The app:

* generates one random token per launch and puts it on ``app.state.security``;
* opens the SQLite event repository at ``Settings.db_path`` on startup and
  closes it on shutdown. Step 2 attaches the runtime's event bus to it, so
  every event the pipeline emits is persisted for replay;
* exposes routes under a router that depends on :func:`require_token`, so a new
  route is protected by default.

The bind address is **not** the app's business — :mod:`backend.api.__main__`
hard-codes ``127.0.0.1`` with no flag to change it. The app additionally
refuses non-loopback ``Host`` headers, so even a misconfigured launcher cannot
serve the API to the network.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend import __version__
from backend.api.event_store import EventStore
from backend.api.middleware import BodySizeLimitMiddleware
from backend.api.models import EventResponse, EventsPageResponse, HealthResponse
from backend.api.repository import SqliteEventRepository
from backend.api.security import ALLOWED_ORIGINS, APP_SCHEME, LaunchSecurity, require_token
from backend.core.config import Settings, get_settings, load_env

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


def create_app(
    *,
    root: Path | str | None = None,
    security: LaunchSecurity | None = None,
    token: str | None = None,
) -> FastAPI:
    """Build the app. Tests pass ``root=tmp_path`` to keep all state in tmp."""
    settings = Settings.from_root(root) if root is not None else None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # The launcher resolves the real Settings; tests that injected a root
        # already have one. Either way the db lives at Settings.db_path
        # (data/app.db) — never beside the code, never in the workspace.
        # load_env first so .env wins over the shell (D12).
        load_env(app.state.root)
        resolved = settings or get_settings(app.state.root)
        app.state.settings = resolved
        app.state.event_store = EventStore(SqliteEventRepository(resolved.db_path))
        # Phase 4 step 2 attaches the runtime's bus here; until then the bus is
        # created on first run so the event store is ready first.
        logger.info("API ready: db=%s", resolved.db_path)
        try:
            yield
        finally:
            app.state.event_store.close()

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

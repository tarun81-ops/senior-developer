"""Per-launch API security (D17).

The server is a local tool that runs model-authored code, so three independent
checks guard every request:

1. **A random token** (``X-API-Key``), generated once per process launch with
   :mod:`secrets`. It lives only in the serving process; the desktop shell reads
   it from the launch handshake. A browser page on another site cannot guess it,
   so the "approve this command?" dialog cannot be forged from a random tab.
2. **A Host header check** — only loopback host names are served, which blocks
   DNS-rebinding attacks (a hostile page resolving its own name to 127.0.0.1).
3. **An Origin allowlist for CORS** — only the Vite dev servers and our own
   ``sda://app`` desktop origin may talk to the API. CORS is a browser rule, not auth,
   so it is defence in depth *on top of* the token, never instead of it.

The token is intentionally not configurable through the environment: a token
pinned in a file would outlive the process that generated it.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field

from fastapi import Header, HTTPException, Request, status

#: Header carrying the per-launch token on every request.
API_KEY_HEADER = "X-API-Key"

#: The only browser origins the API answers, matched exactly (no prefixes,
#: no patterns) by both the Origin check and CORS.
#:
#: * ``http://127.0.0.1:5173`` / ``http://localhost:5173`` — Vite's dev server.
#: * ``sda://app`` — the packaged desktop UI. ``sda`` ("senior developer
#:   agents") is a custom protocol that Part B registers in Electron *before*
#:   ``app.ready`` with ``protocol.registerSchemesAsPrivileged`` and the
#:   privileges ``standard``, ``secure``, ``supportFetchAPI`` and
#:   ``corsEnabled``, then serves ``desktop/dist`` from ``sda://app/…``. A standard
#:   scheme gives the renderer a real origin, ``scheme://host``; loading over
#:   ``file://`` would report ``Origin: null`` instead, which every sandboxed
#:   iframe and ``data:`` page also sends, so ``null`` stays denied. Part B
#:   must use exactly this scheme and host; change both sides together.
APP_ORIGIN = "sda://app"
ALLOWED_ORIGINS: tuple[str, ...] = (
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    APP_ORIGIN,
)

#: Host header values we answer to. The port is whatever uvicorn is told to use,
#: so it is checked separately from the host name.
_ALLOWED_HOSTS: frozenset[str] = frozenset(
    {
        "127.0.0.1",
        "localhost",
        "[::1]",
        "::1",
    }
)

#: Largest request body accepted, in bytes. Run requests are short prose; a file
#: upload is never legitimate, so a small cap costs nothing and bounds memory.
MAX_REQUEST_BYTES = 256 * 1024


def new_api_token() -> str:
    """A fresh, URL-safe 256-bit token for this launch."""
    return secrets.token_urlsafe(32)


def is_allowed_host(host_header: str | None) -> bool:
    """True when ``Host`` names the loopback interface (on any port)."""
    if not host_header:
        return False
    if host_header.startswith("["):
        # bracketed IPv6 literal, optionally followed by ":port"
        closing = host_header.find("]")
        if closing == -1:
            return False
        name = host_header[: closing + 1]
        rest = host_header[closing + 1 :]
        if rest and not rest.startswith(":"):
            return False
    else:
        name, sep, port = host_header.partition(":")
        if sep and not port.isdigit():
            return False
    return name.lower() in _ALLOWED_HOSTS


def is_allowed_origin(origin: str | None) -> bool:
    """True when ``Origin`` is exactly one of :data:`ALLOWED_ORIGINS`.

    A request with no ``Origin`` (Electron main process, curl, the test client)
    is *not* rejected here; only a *present, unknown* origin is a violation.

    ``Origin: null`` is rejected on purpose. It is what an Electron renderer
    loaded over ``file://`` reports, but it is also what every sandboxed iframe
    and ``data:`` page reports, so allowing it would allow all of them. Part B
    avoids the problem instead of permitting it: the shell registers the custom
    ``sda:`` scheme and loads the built UI from ``sda://app/…``.
    """
    return origin is None or origin in ALLOWED_ORIGINS


def _too_large(received: int | None = None) -> HTTPException:
    """The single 413 body both the header check and the byte counter return."""
    where = f"Request body is at least {received} bytes; " if received is not None else ""
    return HTTPException(
        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        detail=(
            f"{where}the limit is {MAX_REQUEST_BYTES} bytes. "
            "Run requests are prose, not file uploads."
        ),
    )



@dataclass(frozen=True)
class LaunchSecurity:
    """The per-launch token plus the immutable allowlists."""

    token: str = field(default_factory=new_api_token)

    def check_host(self, host_header: str | None) -> None:
        if not is_allowed_host(host_header):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Host header is not a loopback address",
            )

    def check_origin(self, origin: str | None) -> None:
        if not is_allowed_origin(origin):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Origin {origin!r} is not allowed",
            )

    def check_token(self, supplied: str | None) -> None:
        # secrets.compare_digest so the check is constant-time; a missing header
        # fails the same way a wrong one does.
        if supplied is None or not secrets.compare_digest(supplied, self.token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid API token",
            )

    def check_body_size(self, request: Request) -> None:
        """Cheap early exit on a declared Content-Length.

        This is *not* the authoritative size limit — a chunked request may
        declare no length at all (or a dishonest one). The real cap is
        :class:`~backend.api.middleware.BodySizeLimitMiddleware`, which counts
        the bytes the server actually receives.
        """
        raw = request.headers.get("content-length")
        if raw is None:
            return
        try:
            length = int(raw)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Content-Length header",
            ) from None
        if length < 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Content-Length header",
            )
        if length > MAX_REQUEST_BYTES:
            raise _too_large(length)


async def require_token(
    request: Request,
    x_api_key: str | None = Header(default=None, alias=API_KEY_HEADER),
) -> None:
    """FastAPI dependency: enforce Host, Origin, body size and the token.

    Attached at the router level so a new route is protected by default; a
    route that must *also* re-check the token (approve/reject) depends on this
    function explicitly, which makes the requirement visible at the call site.
    """
    security: LaunchSecurity = request.app.state.security
    security.check_host(request.headers.get("host"))
    security.check_origin(request.headers.get("origin"))
    security.check_body_size(request)
    security.check_token(x_api_key)


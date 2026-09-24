"""Request-body size limit enforced on bytes actually received.

``Content-Length`` is a claim, not a fact: a chunked request sends no length at
all, and a client may declare 1 byte and stream a gigabyte. A route that only
inspects the header therefore has no size limit at all.

This middleware reads the body *before* handing the request to the application,
counting what actually arrives. Two properties follow:

* **A chunked body is capped.** There is no length header to trust, so the
  running byte count is the only check that can be correct.
* **Memory is bounded.** Reading stops the moment the total passes the cap, so
  an oversized body is never fully buffered; a legitimate body is replayed to
  the app from the buffer.

The ``Content-Length`` check in :mod:`backend.api.security` remains as a cheap
early exit (no body is read at all when the client is upfront about being too
large) and returns the same 413.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from backend.api.security import MAX_REQUEST_BYTES

#: Methods whose bodies count toward the limit. GET/HEAD/DELETE are excluded:
#: the framework ignores their bodies, and the SSE stream lives on GET.
_COUNTED_METHODS = frozenset({"POST", "PUT", "PATCH"})


class BodySizeLimitMiddleware:
    """Reject a request whose received body exceeds ``max_bytes`` with 413."""

    def __init__(self, app: ASGIApp, *, max_bytes: int = MAX_REQUEST_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method", "") not in _COUNTED_METHODS:
            await self.app(scope, receive, send)
            return

        buffered, early, too_large = await self._read_body(receive)
        if too_large:
            await self._send_413(send, len(buffered))
            return
        await self.app(scope, self._replay(buffered, early, receive), send)

    async def _read_body(self, receive: Receive) -> tuple[bytes, Message | None, bool]:
        """Buffer the body, stopping as soon as it passes the cap.

        Returns ``(body, early_message, too_large)``. ``early_message`` is a
        non-body message (``http.disconnect``) that arrived before the body was
        finished and must be replayed first.
        """
        parts: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return b"".join(parts), message, False
            body = message.get("body", b"")
            if body:
                total += len(body)
                if total > self.max_bytes:
                    # Stop reading immediately: an oversized body must never be
                    # fully buffered just to measure it. Keep only the bytes up
                    # to one past the cap so the 413 can name the real total.
                    kept = self.max_bytes + 1 - total + len(body)
                    parts.append(body[:kept])
                    return b"".join(parts), None, True
                parts.append(body)
            if not message.get("more_body", False):
                return b"".join(parts), None, False

    def _replay(self, body: bytes, early: Message | None, receive: Receive) -> Receive:
        """Return a ``receive`` that hands the app the buffered body."""
        pending: list[Message] = []
        if early is not None:
            pending.append(early)
        elif body:
            pending.append({"type": "http.request", "body": body, "more_body": True})
        pending.append({"type": "http.request", "body": b"", "more_body": False})

        async def replay() -> Message:
            if pending:
                return pending.pop(0)
            return await receive()

        return replay

    async def _send_413(self, send: Send, received: int) -> None:
        """Send the 413 directly; the endpoint is never invoked."""
        detail = (
            f"Request body is at least {received} bytes; the limit is "
            f"{self.max_bytes}. Run requests are prose, not file uploads."
        )
        body = detail.encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

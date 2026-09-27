"""The SSE event stream: replay from a cursor, then live, then close (D19, D23).

SQLite is the only source. Replay and live tailing are the same query,
``seq > cursor``, run again whenever the store says something new was written.
The handoff from replay to live therefore has no seam to drop or repeat an
event: the cursor is the last ``seq`` actually sent, and nothing else.

A stream ends after it sends the run's closing event (``api.run_succeeded``,
``api.run_failed`` or ``api.run_cancelled``). The run manager writes that event
in the same critical section that sets the terminal status, and writes nothing
after it, so "closing event sent" means "every event sent".
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator

from starlette.concurrency import run_in_threadpool

from backend.api.event_store import EventStore
from backend.api.models import TERMINAL_STATUSES
from backend.api.repository import StoredEvent
from backend.api.run_manager import RunManager
from backend.core.workspace.terminal import TerminalChunk, TerminalRecord

#: Kinds of the one closing event every managed run, and every deploy (D40), gets.
CLOSING_KINDS = frozenset(f"api.run_{status}" for status in TERMINAL_STATUSES) | {
    "deploy.succeeded",
    "deploy.failed",
}


class StreamOwners:
    """Runs and deploys share the SSE endpoint, each under its own id (D40).

    A deploy streams under its own id, never the run's: nothing may follow a
    run's closing event (D23), and a deploy happens after the run has ended.
    """

    def __init__(self, *owners) -> None:  # noqa: ANN002 - anything with is_finished(id)
        self._owners = owners

    def is_finished(self, stream_id: str) -> bool | None:
        for owner in self._owners:
            finished = owner.is_finished(stream_id)
            if finished is not None:
                return finished
        return None

#: D19: a timestamped comment frame about this often keeps an idle connection warm.
HEARTBEAT_SECONDS = 20.0

#: Rows per replay query; a longer backlog is simply several queries.
PAGE_SIZE = 500


def sse_frame(stored: StoredEvent) -> str:
    """One SSE message. ``id`` is the seq, so ``Last-Event-ID`` resumes it."""
    return f"id: {stored.seq}\ndata: {json.dumps(stored.to_dict())}\n\n"


async def event_frames(
    store: EventStore,
    manager: RunManager,
    run_id: str,
    *,
    after_seq: int = -1,
    heartbeat: float = HEARTBEAT_SECONDS,
) -> AsyncIterator[str]:
    """Yield SSE frames for ``run_id`` after ``after_seq`` until the run ends.

    Blocking store reads and waits run in the thread pool, never on the loop.
    """
    cursor = after_seq
    last_write = time.monotonic()
    while True:
        # Read "finished?" *before* the query: if the run was already over,
        # its closing event is persisted and this query returns it.
        finished = manager.is_finished(run_id)
        page = await run_in_threadpool(store.replay, run_id, after_seq=cursor, limit=PAGE_SIZE)
        for stored in page:
            yield sse_frame(stored)
            cursor = stored.seq
            if stored.kind in CLOSING_KINDS:
                return
        if page:
            last_write = time.monotonic()
        if len(page) == PAGE_SIZE:
            continue
        if finished is not False:
            # Caught up and nothing more can arrive: the run is over (closing
            # event already sent on an earlier connection) or it belongs to an
            # earlier process and only its history exists.
            return
        await run_in_threadpool(store.wait_for, run_id, after_seq=cursor)
        if time.monotonic() - last_write >= heartbeat:
            yield f": keepalive {int(time.time())}\n\n"
            last_write = time.monotonic()


def terminal_frame(chunk: TerminalChunk) -> str:
    """One SSE message for one terminal chunk (D43). ``id`` resumes it, same
    as :func:`sse_frame` does for a run event."""
    return f"id: {chunk.seq}\ndata: {json.dumps(chunk.to_dict())}\n\n"


async def terminal_frames(
    record: TerminalRecord,
    *,
    after_seq: int,
    heartbeat: float = HEARTBEAT_SECONDS,
) -> AsyncIterator[str]:
    """Yield SSE frames of terminal output after ``after_seq``, forever.

    Unlike :func:`event_frames`, this never closes on its own — a shell
    exiting is just another chunk (``closed: true``), not the end of the
    stream: the next submitted command transparently starts a fresh session
    and the same stream keeps delivering its output. The connection ends
    only when the client disconnects.

    ``record.replay_and_wait`` already blocks for up to ``heartbeat``
    seconds, so an empty page *is* the heartbeat tick — there is no separate
    idle timer to run here, unlike the run-event stream, which has to poll a
    SQLite ``wait_for`` and a real clock.
    """
    cursor = after_seq
    while True:
        page = await run_in_threadpool(record.replay_and_wait, cursor, timeout=heartbeat)
        if not page:
            yield f": keepalive {int(time.time())}\n\n"
            continue
        for chunk in page:
            yield terminal_frame(chunk)
            cursor = chunk.seq

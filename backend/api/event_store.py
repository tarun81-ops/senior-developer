"""Bridge from :class:`EventBus` to the event repository (D18).

The existing :class:`~backend.core.events.EventBus` is the contract: the CLI
prints its events, the JSONL writer records them, and Phase 4's API simply
subscribes to the same bus. Nothing in ``backend.core`` changes.

This service adds the two things the HTTP layer needs:

* **Sequence numbers.** JSONL has no ids; a reconnecting SSE client needs
  "give me everything after seq N", so each run's events are numbered
  ``0, 1, 2, …`` as they pass through here.
* **A change signal.** SSE readers wait on a :class:`threading.Condition`
  instead of polling the database in a tight loop; the pipeline thread wakes
  them the moment it emits.

The sink is synchronous and runs on whichever thread emitted the event (the
background pipeline thread). SQLite writes are sub-millisecond for a single
row and WAL makes readers non-blocking, so persisting inline keeps the order
of events exactly as the pipeline produced them — the alternative (an
``asyncio.Queue`` fed from a worker thread) would add a thread-safe queue and a
second ordering story for no benefit.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from backend.api.repository import EventRepository, StoredEvent
from backend.core.events import Event, EventBus

#: How long an SSE reader waits before re-checking "is the run still active?"
#: even with no new events, so it can send a heartbeat and notice a finished run.
DEFAULT_POLL_SECONDS = 1.0


class EventStore:
    """Subscribes to event buses and persists everything they emit."""

    def __init__(self, repository: EventRepository) -> None:
        self.repository = repository
        self._lock = threading.Lock()
        self._seqs: dict[str, int] = {}
        self._condition = threading.Condition(self._lock)
        self._unsubscribes: list[Callable[[], None]] = []

    # -- wiring --------------------------------------------------------------
    def subscribe(self, bus: EventBus) -> Callable[[], None]:
        """Persist every event ``bus`` emits until the returned callable is called."""
        unsubscribe = bus.subscribe(self.sink)
        self._unsubscribes.append(unsubscribe)
        return unsubscribe

    def close(self) -> None:
        for unsubscribe in self._unsubscribes:
            unsubscribe()
        self._unsubscribes.clear()
        self.repository.close()

    # -- writing -------------------------------------------------------------
    def sink(self, event: Event) -> None:
        """EventBus sink: assign a sequence number, persist, wake readers."""
        # One critical section for number + write: two threads emitting for the
        # same run (the worker and a cancel request) must never make seq N+1
        # visible before N, or a streaming reader would skip N for good.
        with self._condition:
            if event.run_id not in self._seqs:
                # A run from an earlier launch continues after its saved
                # events; restarting at 0 would collide, and INSERT OR IGNORE
                # would drop the new event silently (D32).
                self._seqs[event.run_id] = self.repository.last_seq(event.run_id)
            seq = self._seqs[event.run_id] + 1
            self._seqs[event.run_id] = seq
            self.repository.append(event, seq=seq)
            self._condition.notify_all()

    # -- reading -------------------------------------------------------------
    def replay(
        self, run_id: str, *, after_seq: int = -1, limit: int = 500
    ) -> list[StoredEvent]:
        """Persisted events after a cursor — the reconnect path for a client."""
        return self.repository.get_events(run_id, after_seq=after_seq, limit=limit)

    def last_seq(self, run_id: str) -> int:
        return self.repository.last_seq(run_id)

    def save_run(self, run_id: str, created_at: str, record: dict) -> None:
        self.repository.save_run(run_id, created_at, record)

    def load_runs(self, *, limit: int) -> list[dict]:
        return self.repository.load_runs(limit=limit)

    def save_deploy(self, deploy_id: str, run_id: str, created_at: str, record: dict) -> None:
        self.repository.save_deploy(deploy_id, run_id, created_at, record)

    def load_deploys(self, *, limit: int) -> list[dict]:
        return self.repository.load_deploys(limit=limit)

    def run_ids(self, *, limit: int = 100) -> list[str]:
        return self.repository.run_ids(limit=limit)

    def wait_for(
        self, run_id: str, *, after_seq: int, timeout: float = DEFAULT_POLL_SECONDS
    ) -> None:
        """Block until an event after ``after_seq`` exists, or ``timeout`` elapses.

        Called from a worker thread (an SSE generator runs the repository reads
        in a thread pool), so the wait never blocks the event loop.
        """
        with self._condition:
            if self.repository.last_seq(run_id) > after_seq:
                return
            self._condition.wait(timeout)

    def wake(self) -> None:
        """Wake every waiter regardless of new events (shutdown, cancellation)."""
        with self._condition:
            self._condition.notify_all()

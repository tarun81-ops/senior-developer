"""The event bus: one place every agent action flows through.

Design rule from the requirements: *every agent action is logged so the UI can
show it live*. So nothing calls ``print`` or ``logging`` directly. Agents and
the provider router emit structured events, and the bus fans them out to:

  * a JSONL file on disk (history, replay after a crash)
  * the terminal (so Phase 1 is watchable without any UI)
  * any subscriber (Phase 4 registers one that pushes to a WebSocket)

Event kinds used in Phase 1:

    run.start / run.end        a CLI command started / finished
    agent.start / agent.end    an agent began / finished thinking
    provider.attempt           about to call a provider (attempt n of m)
    provider.retry             retryable failure, sleeping before retry
    provider.rate_limited      HTTP 429 (with Retry-After if given)
    provider.failover          giving up on a provider, switching to next
    provider.skipped           provider not usable (no key, cooldown, quota)
    llm.response               successful completion, with tokens and latency
    error                      something unrecoverable for this run
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from backend.core.events.jsonl_log import JsonlWriter

EventSink = Callable[["Event"], None]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class Event:
    """One thing that happened, in a shape both humans and the UI can read."""

    kind: str
    message: str
    run_id: str = "-"
    agent: str | None = None
    provider: str | None = None
    model: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    ts: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventBus:
    """Fan-out for events: disk, terminal, and in-process subscribers."""

    def __init__(
        self,
        *,
        run_id: str = "-",
        jsonl: JsonlWriter | None = None,
        echo: bool = True,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.run_id = run_id
        self.jsonl = jsonl
        self.echo = echo
        self._subscribers: list[EventSink] = []
        self._lock = threading.Lock()
        self._clock = clock or _now_iso

    # -- subscription -------------------------------------------------------
    def subscribe(self, sink: EventSink) -> Callable[[], None]:
        """Register a callback; returns a function that unsubscribes it."""
        with self._lock:
            self._subscribers.append(sink)

        def unsubscribe() -> None:
            with self._lock:
                if sink in self._subscribers:
                    self._subscribers.remove(sink)

        return unsubscribe

    # -- emitting -----------------------------------------------------------
    def emit(
        self,
        kind: str,
        message: str,
        *,
        agent: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        **data: Any,
    ) -> Event:
        event = Event(
            kind=kind,
            message=message,
            run_id=self.run_id,
            agent=agent,
            provider=provider,
            model=model,
            data=data,
            ts=self._clock(),
        )
        self._dispatch(event)
        return event

    def emit_event(self, event: Event) -> Event:
        self._dispatch(event)
        return event

    def _dispatch(self, event: Event) -> None:
        if self.jsonl is not None:
            self.jsonl.write(event.to_dict())
        with self._lock:
            sinks = list(self._subscribers)
        for sink in sinks:
            try:
                sink(event)
            except Exception:  # a broken listener must never kill a run
                continue
        if self.echo:
            print(format_event(event))

    # -- convenience --------------------------------------------------------
    def child(self, *, run_id: str | None = None) -> EventBus:
        """A bus that shares sinks/writer but tags events with another run id."""
        clone = EventBus(
            run_id=run_id or self.run_id,
            jsonl=self.jsonl,
            echo=self.echo,
            clock=self._clock,
        )
        with self._lock:
            clone._subscribers = list(self._subscribers)
        return clone


def format_event(event: Event, *, width: int = 34) -> str:
    """One-line terminal rendering of an event."""
    stamp = event.ts[11:23] if len(event.ts) >= 23 else event.ts
    target = ""
    if event.provider:
        target = f"{event.provider}/{event.model}" if event.model else event.provider
    who = f"[{event.agent}] " if event.agent else ""
    return f"{stamp}  {event.kind:<19} {target:<{width}} {who}{event.message}"

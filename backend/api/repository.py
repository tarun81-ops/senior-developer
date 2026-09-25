"""Storage layer for the API: events and, later, runs.

Every SQL statement lives behind :class:`EventRepository`. The FastAPI routes
and the pipeline manager only ever see the protocol, so swapping SQLite for a
cloud database in Phase 5 means writing one new implementation, not touching
endpoints (D18).

Two rules the storage layer follows:

* **One writer, many readers.** The pipeline runs in a background thread and
  SSE readers poll from the event loop; SQLite's WAL mode plus a short
  ``busy_timeout`` lets those coexist without a lock in the request path.
* **Events are append-only and idempotent.** A run's events carry a
  monotonically increasing ``seq`` (per run) and the ``(run_id, seq)`` pair is
  unique, so replaying a JSONL file into the database can never double-insert.

This module contains no HTTP concepts and no pipeline concepts: it is a
repository, nothing else.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from backend.core.events import Event

#: Applied on every connection; SQLite is stdlib, there is no migration tool.
SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    run_id TEXT NOT NULL,
    seq    INTEGER NOT NULL,
    ts     TEXT NOT NULL,
    kind   TEXT NOT NULL,
    message TEXT NOT NULL,
    agent TEXT,
    provider TEXT,
    model TEXT,
    data_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (run_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_events_run_ts ON events (run_id, ts);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    record_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs (created_at);
CREATE TABLE IF NOT EXISTS deploys (
    deploy_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    record_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deploys_created ON deploys (created_at);
"""


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class StoredEvent:
    """One persisted event plus the id needed to resume a stream after it."""

    run_id: str
    seq: int
    ts: str
    kind: str
    message: str
    agent: str | None = None
    provider: str | None = None
    model: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_event(cls, event: Event, *, seq: int) -> StoredEvent:
        return cls(
            run_id=event.run_id,
            seq=seq,
            ts=event.ts or _now_iso(),
            kind=event.kind,
            message=event.message,
            agent=event.agent,
            provider=event.provider,
            model=event.model,
            data=dict(event.data),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "seq": self.seq,
            "ts": self.ts,
            "kind": self.kind,
            "message": self.message,
            "agent": self.agent,
            "provider": self.provider,
            "model": self.model,
            "data": self.data,
        }


@runtime_checkable
class EventRepository(Protocol):
    """The only persistence interface the API depends on."""

    def append(self, event: Event, *, seq: int) -> None:
        """Persist one event under the given per-run sequence number."""

    def save_run(self, run_id: str, created_at: str, record: dict[str, Any]) -> None:
        """Insert or replace one run's latest snapshot (D32)."""

    def load_runs(self, *, limit: int) -> list[dict[str, Any]]:
        """The newest ``limit`` run snapshots, oldest first."""

    def save_deploy(
        self, deploy_id: str, run_id: str, created_at: str, record: dict[str, Any]
    ) -> None:
        """Insert or replace one deploy's latest snapshot (D40)."""

    def load_deploys(self, *, limit: int) -> list[dict[str, Any]]:
        """The newest ``limit`` deploy snapshots, oldest first."""


class SqliteEventRepository:
    """SQLite-backed :class:`EventRepository` (one database, many tables).

    Each operation opens its own short-lived connection. That is deliberate:
    the background pipeline thread and the asyncio event loop both touch this
    database, and a connection is not shareable between them.
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_lock = threading.Lock()
        self._initialised = False

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=5.0,  # seconds to wait for a concurrent writer
            check_same_thread=False,
            isolation_level=None,  # explicit transactions, no implicit ones
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _ensure_schema(self) -> None:
        with self._init_lock:
            if self._initialised:
                return
            with self._connect() as conn:
                conn.executescript(SCHEMA)
            self._initialised = True

    def append(self, event: Event, *, seq: int) -> None:
        """Insert one event; a duplicate ``(run_id, seq)`` is ignored.

        ``INSERT OR IGNORE`` rather than ``INSERT`` because replaying a run's
        JSONL into the database is an explicit, supported operation, and it must
        be safe to run twice.
        """
        self._ensure_schema()
        stored = StoredEvent.from_event(event, seq=seq)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO events
                    (run_id, seq, ts, kind, message, agent, provider, model, data_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stored.run_id,
                    stored.seq,
                    stored.ts,
                    stored.kind,
                    stored.message,
                    stored.agent,
                    stored.provider,
                    stored.model,
                    json.dumps(stored.data, ensure_ascii=False, default=str),
                ),
            )

    def append_many(self, events: list[tuple[Event, int]]) -> None:
        """Append several ``(event, seq)`` pairs in one transaction."""
        if not events:
            return
        self._ensure_schema()
        rows = []
        for event, seq in events:
            stored = StoredEvent.from_event(event, seq=seq)
            rows.append(
                (
                    stored.run_id,
                    stored.seq,
                    stored.ts,
                    stored.kind,
                    stored.message,
                    stored.agent,
                    stored.provider,
                    stored.model,
                    json.dumps(stored.data, ensure_ascii=False, default=str),
                )
            )
        with self._connect() as conn:
            conn.execute("BEGIN")
            conn.executemany(
                """
                INSERT OR IGNORE INTO events
                    (run_id, seq, ts, kind, message, agent, provider, model, data_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.execute("COMMIT")

    def get_events(
        self, run_id: str, *, after_seq: int = -1, limit: int = 500
    ) -> list[StoredEvent]:
        """Replay window: events with ``seq > after_seq``, oldest first."""
        self._ensure_schema()
        limit = max(1, min(int(limit), 5000))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM events
                WHERE run_id = ? AND seq > ?
                ORDER BY seq ASC
                LIMIT ?
                """,
                (run_id, after_seq, limit),
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def last_seq(self, run_id: str) -> int:
        self._ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(seq) AS seq FROM events WHERE run_id = ?", (run_id,)
            ).fetchone()
        return -1 if row is None or row["seq"] is None else int(row["seq"])

    def run_ids(self, *, limit: int = 100) -> list[str]:
        self._ensure_schema()
        limit = max(1, min(int(limit), 1000))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT run_id, MAX(seq) AS last_seq, MAX(ts) AS last_ts
                FROM events
                GROUP BY run_id
                ORDER BY last_seq DESC, last_ts DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [row["run_id"] for row in rows]

    # -- runs (D32) ------------------------------------------------------------
    def save_run(self, run_id: str, created_at: str, record: dict[str, Any]) -> None:
        """Insert or replace one run's latest snapshot. Keys are never in it."""
        self._ensure_schema()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO runs (run_id, created_at, record_json) VALUES (?, ?, ?)",
                (run_id, created_at, json.dumps(record, default=str)),
            )

    def load_runs(self, *, limit: int) -> list[dict[str, Any]]:
        """The newest ``limit`` run snapshots, oldest first (the order runs were made)."""
        self._ensure_schema()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT record_json FROM runs ORDER BY created_at DESC, run_id DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [json.loads(row["record_json"]) for row in reversed(rows)]

    # -- deploys (D40) ---------------------------------------------------------
    def save_deploy(
        self, deploy_id: str, run_id: str, created_at: str, record: dict[str, Any]
    ) -> None:
        """Insert or replace one deploy's latest snapshot. Tokens are never in it."""
        self._ensure_schema()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO deploys (deploy_id, run_id, created_at, record_json) "
                "VALUES (?, ?, ?, ?)",
                (deploy_id, run_id, created_at, json.dumps(record, default=str)),
            )

    def load_deploys(self, *, limit: int) -> list[dict[str, Any]]:
        """The newest ``limit`` deploy snapshots, oldest first."""
        self._ensure_schema()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT record_json FROM deploys ORDER BY created_at DESC, deploy_id DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [json.loads(row["record_json"]) for row in reversed(rows)]

    def close(self) -> None:
        """No pooled connections are held, so this is a no-op kept for the protocol."""
        return None

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> StoredEvent:
        return StoredEvent(
            run_id=row["run_id"],
            seq=int(row["seq"]),
            ts=row["ts"],
            kind=row["kind"],
            message=row["message"],
            agent=row["agent"],
            provider=row["provider"],
            model=row["model"],
            data=json.loads(row["data_json"]) if row["data_json"] else {},
        )

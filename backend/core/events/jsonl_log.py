"""Append-only JSONL writer for run events.

One file per run:  ``data/runs/<run_id>/events.jsonl``

Why JSONL (one JSON object per line)? It is append-only, human readable,
crash-safe (a half-written last line is simply skipped) and trivial to tail
from the Phase 4 UI over a WebSocket.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any


class JsonlWriter:
    """Thread-safe append-only JSONL writer."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def read_all(self) -> list[dict[str, Any]]:
        """Return every well-formed record in the file."""
        return list(iter_records(self.path))

    def tail(self, count: int = 20) -> list[dict[str, Any]]:
        records = self.read_all()
        return records[-count:] if count > 0 else records


def iter_records(path: Path | str) -> Iterator[dict[str, Any]]:
    """Yield records, skipping malformed lines (e.g. a crash mid-write)."""
    path = Path(path)
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


def find_run_events(settings_runs_dir: Path | str, run_id: str | None = None) -> Path | None:
    """Locate an events file, defaulting to the most recent run."""
    runs_dir = Path(settings_runs_dir)
    if run_id:
        candidate = runs_dir / run_id / "events.jsonl"
        return candidate if candidate.exists() else None
    if not runs_dir.exists():
        return None
    candidates = sorted(runs_dir.glob("*/events.jsonl"), key=lambda p: p.stat().st_mtime)
    return candidates[-1] if candidates else None

"""Hard ceilings for a single run.

Free tiers are small and agents can loop. Without a ceiling, one bug can burn a
day of quota in minutes and you have no idea why. So every run gets a budget:

  * max model calls
  * max tokens

The counters are persisted to ``data/runs/<run_id>/budget.json``, which means a
crashed run can be inspected, and a resumed run (Phase 6) keeps its history.

Exceeding a budget raises :class:`BudgetExceeded`. The orchestrator's job is to
catch that and stop, not to keep retrying: at that point a human decides.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.core.errors import BudgetExceeded
from backend.core.provider.registry import BudgetConfig


@dataclass
class RunState:
    run_id: str
    calls: int = 0
    tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "calls": self.calls,
            "tokens": self.tokens,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
        }


class BudgetTracker:
    """Counts calls and tokens for one run and refuses to go past the ceiling."""

    def __init__(
        self,
        config: BudgetConfig,
        *,
        run_id: str,
        path: Path | str | None = None,
        state: RunState | None = None,
    ) -> None:
        self.config = config
        self.run_id = run_id
        self.path = Path(path) if path else None
        self.state = state or RunState(run_id=run_id)
        self._lock = threading.RLock()

    # -- persistence --------------------------------------------------------
    @classmethod
    def load(
        cls,
        config: BudgetConfig,
        *,
        run_id: str,
        path: Path | str,
    ) -> BudgetTracker:
        path = Path(path)
        state = RunState(run_id=run_id)
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                state = RunState(
                    run_id=run_id,
                    calls=int(raw.get("calls") or 0),
                    tokens=int(raw.get("tokens") or 0),
                    prompt_tokens=int(raw.get("prompt_tokens") or 0),
                    completion_tokens=int(raw.get("completion_tokens") or 0),
                    started_at=str(raw.get("started_at") or state.started_at),
                    updated_at=str(raw.get("updated_at") or ""),
                )
            except (OSError, json.JSONDecodeError):
                pass
        return cls(config, run_id=run_id, path=path, state=state)

    def _flush(self) -> None:
        if self.path is None:
            return
        self.state.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state.to_dict(), indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # -- accounting ---------------------------------------------------------
    @property
    def calls_used(self) -> int:
        return self.state.calls

    @property
    def tokens_used(self) -> int:
        return self.state.tokens

    def begin_call(self) -> None:
        """Reserve one model call, or raise if the run is already over budget."""
        with self._lock:
            if self.state.calls >= self.config.max_calls_per_run:
                raise BudgetExceeded(
                    f"Run hit the maximum of {self.config.max_calls_per_run} model calls",
                    calls=self.state.calls,
                    tokens=self.state.tokens,
                )
            if self.state.tokens >= self.config.max_tokens_per_run:
                raise BudgetExceeded(
                    f"Run hit the maximum of {self.config.max_tokens_per_run} tokens",
                    calls=self.state.calls,
                    tokens=self.state.tokens,
                )
            self.state.calls += 1
            self._flush()

    def add_usage(self, *, prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
        total = max(0, prompt_tokens) + max(0, completion_tokens)
        with self._lock:
            self.state.prompt_tokens += max(0, prompt_tokens)
            self.state.completion_tokens += max(0, completion_tokens)
            self.state.tokens += total
            self._flush()

    def summary(self) -> str:
        return (
            f"{self.state.calls}/{self.config.max_calls_per_run} calls, "
            f"{self.state.tokens}/{self.config.max_tokens_per_run} tokens"
        )

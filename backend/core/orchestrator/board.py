"""The shared task board: what every stage produced, and what feeds the next.

Why a board instead of passing values between function calls: the orchestrator,
the CLI and (in Phase 4) the UI all need the same answer to "where are we and
what does each stage have". The board is that single answer, persisted to
``data/runs/<run_id>/board.json`` in the same atomic-write style as
``budget.json``, so a crashed run can be inspected and a later UI can read it
without replaying the event log.

Stage lifecycle::

    pending -> running -> done
                      \\-> failed   (provider/budget/config error; run stops)
    pending -> skipped             (stage never ran because an earlier one failed
                                    or the review verdict stopped the pipeline)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class StageRecord:
    """One stage's state and artifact on the board."""

    stage: str
    agent: str = ""
    status: str = PENDING
    #: provider/model that actually answered ("" until the stage completes)
    target: str = ""
    #: how many times this stage has run (fix iterations re-run coder/reviewer)
    attempts: int = 0
    text: str = ""
    parsed: dict[str, Any] | None = None
    tokens: int = 0
    latency_ms: int = 0
    error: str = ""
    #: human-readable notes, e.g. "reviewer output had no JSON block"
    notes: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "agent": self.agent,
            "status": self.status,
            "target": self.target,
            "attempts": self.attempts,
            "text": self.text,
            "parsed": self.parsed,
            "tokens": self.tokens,
            "latency_ms": self.latency_ms,
            "error": self.error,
            "notes": list(self.notes),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> StageRecord:
        return cls(
            stage=str(raw.get("stage") or ""),
            agent=str(raw.get("agent") or ""),
            status=str(raw.get("status") or PENDING),
            target=str(raw.get("target") or ""),
            attempts=int(raw.get("attempts") or 0),
            text=str(raw.get("text") or ""),
            parsed=raw.get("parsed"),
            tokens=int(raw.get("tokens") or 0),
            latency_ms=int(raw.get("latency_ms") or 0),
            error=str(raw.get("error") or ""),
            notes=[str(n) for n in raw.get("notes") or []],
            started_at=str(raw.get("started_at") or ""),
            finished_at=str(raw.get("finished_at") or ""),
        )



class TaskBoard:
    """Ordered stage records for one pipeline run, persisted atomically."""

    def __init__(
        self,
        *,
        run_id: str,
        goal: str,
        stages: list[str],
        agents: dict[str, str] | None = None,
        records: dict[str, StageRecord] | None = None,
        order: list[str] | None = None,
        created_at: str = "",
        updated_at: str = "",
        path: Path | None = None,
    ) -> None:
        self.run_id = run_id
        self.goal = goal
        self.path = path
        self.created_at = created_at or _now()
        self.updated_at = updated_at
        self.order: list[str] = list(order or stages)
        self.records: dict[str, StageRecord] = records or {}
        for stage in stages:
            if stage not in self.records:
                self.records[stage] = StageRecord(
                    stage=stage, agent=(agents or {}).get(stage, "")
                )

    # -- construction -------------------------------------------------------
    @classmethod
    def new(
        cls, *, run_id: str, goal: str, stages: list[str], agents: dict[str, str]
    ) -> TaskBoard:
        return cls(run_id=run_id, goal=goal, stages=stages, agents=agents)

    @classmethod
    def load(cls, path: Path | str) -> TaskBoard:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        records = {
            name: StageRecord.from_dict(entry)
            for name, entry in (raw.get("records") or {}).items()
        }
        board = cls(
            run_id=str(raw.get("run_id") or "-"),
            goal=str(raw.get("goal") or ""),
            stages=[],
            records=records,
            order=[str(s) for s in raw.get("order") or []],
            created_at=str(raw.get("created_at") or ""),
            updated_at=str(raw.get("updated_at") or ""),
            path=Path(path),
        )
        for stage in board.order:
            if stage not in board.records:
                board.records[stage] = StageRecord(stage=stage)
        return board

    # -- persistence --------------------------------------------------------
    def save(self, path: Path | str | None = None) -> Path:
        """Write the board atomically. The first call must supply a path."""
        target = path or self.path
        if target is None:
            # Refusing beats guessing: a relative default would drop board.json
            # into the current working directory.
            raise ValueError(
                "TaskBoard.save() needs a path the first time: pass one, or set board.path"
            )
        target = Path(target)
        if target.suffix == "":
            target = target / "board.json"
        self.path = target
        self.updated_at = _now()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(target)
        return target

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "goal": self.goal,
            "order": list(self.order),
            "records": {name: rec.to_dict() for name, rec in self.records.items()},
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    # -- accessors ----------------------------------------------------------
    def get(self, stage: str) -> StageRecord:
        if stage not in self.records:
            self.records[stage] = StageRecord(stage=stage)
        return self.records[stage]

    def artifact(self, stage: str) -> str | None:
        """Raw output text of a completed stage, for the next stage's context."""
        record = self.records.get(stage)
        if record is None or record.status != DONE or not record.text:
            return None
        return record.text

    def artifacts_for(self, stages: list[str] | tuple[str, ...]) -> dict[str, str]:
        return {
            stage: text
            for stage in stages
            if (text := self.artifact(stage)) is not None
        }

    def verdict(self) -> str | None:
        """The reviewer's last verdict, normalised, or None if never reviewed."""
        record = self.records.get("reviewer")
        if record is None or not isinstance(record.parsed, dict):
            return None
        verdict = str(record.parsed.get("verdict") or "").strip().lower()
        return verdict or None

    # -- mutations (each keeps the on-disk board fresh) ---------------------
    def start(self, stage: str, *, agent: str) -> StageRecord:
        record = self.get(stage)
        record.agent = agent or record.agent
        record.status = RUNNING
        record.attempts += 1
        record.started_at = record.started_at or _now()
        record.error = ""
        self.save()
        return record

    def complete(
        self,
        stage: str,
        *,
        text: str,
        parsed: dict[str, Any] | None,
        target: str,
        tokens: int,
        latency_ms: int,
        note: str | None = None,
    ) -> StageRecord:
        record = self.get(stage)
        record.status = DONE
        record.text = text
        record.parsed = parsed
        record.target = target
        record.tokens += tokens
        record.latency_ms = latency_ms
        record.finished_at = _now()
        if note:
            record.notes.append(note)
        self.save()
        return record

    def fail(self, stage: str, *, error: str) -> StageRecord:
        record = self.get(stage)
        record.status = FAILED
        record.error = error
        record.finished_at = _now()
        self.save()
        return record

    def skip(self, stage: str) -> StageRecord:
        record = self.get(stage)
        if record.status == PENDING:
            record.status = SKIPPED
            self.save()
        return record

    def skip_rest(self, *, after: str | None = None) -> None:
        """Mark every not-yet-run stage as skipped (failure or review stop)."""
        seen = after is None
        for stage in self.order:
            if seen and self.get(stage).status == PENDING:
                self.skip(stage)
            if stage == after:
                seen = True
        self.save()

    # -- summaries ----------------------------------------------------------
    @property
    def ok(self) -> bool:
        return all(rec.status == DONE for rec in self.records.values())

    @property
    def failed_stage(self) -> str | None:
        for name in self.order:
            if self.records[name].status == FAILED:
                return name
        return None

    def rows(self) -> list[dict[str, Any]]:
        """One row per stage, in pipeline order (for CLI tables / the UI)."""
        return [self.records[name].to_dict() for name in self.order]

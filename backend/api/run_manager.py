"""Run manager: one run at a time, always inspectable, always cancellable.

D20 — *one active run at a time*. The API keeps a FIFO queue behind a **single
worker thread**. A second ``POST /api/runs`` is not refused: it is accepted with
``202`` and ``queue_position``, and starts when the run ahead of it finishes.
Serialisation is the worker's job, not the route's, so the ordering is the same
whether a run arrives from the UI, the CLI or a test.

The pipeline is synchronous, blocking code (``Pipeline.run`` → agents → HTTP
provider calls). Running it in a thread is not a stylistic choice: on the event
loop it would block every other request for the whole run, including the
``GET /api/runs/{id}`` the UI polls to show progress. So the manager owns one
worker thread at a time, and **the slot is released in a ``finally``** — success,
failure, cancellation, or an exception during setup all go through the same
exit, so a crashed run can never wedge the queue.

Cancellation is cooperative plus forceful:

* ``record.cancel_requested`` is a ``threading.Event`` the pipeline checks at its
  own checkpoints, so an in-flight provider call is not torn out from under the
  router.
* A run cancelled *before it starts* never builds a runtime and never calls a
  provider: the worker returns the moment it picks the record up.
* If a generated command is running at that moment, the runner's own cancel hook
  fires and :func:`backend.core.workspace.runner._kill_tree` kills the **whole
  child process tree** with ``taskkill /T /F`` on Windows (D15).
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC
from functools import partial
from pathlib import Path
from typing import Any

from backend.api.models import (
    MAX_FILES_IN_RESPONSE,
    TERMINAL_STATUSES,
    AgentOutput,
    RunOptions,
    RunStateResponse,
    RunStatus,
    RunSummary,
    StageState,
    WrittenFile,
    utc_now,
)
from backend.core.config import Settings
from backend.core.errors import AgentSystemError, ApprovalRejected, RunCancelled
from backend.core.events import Event
from backend.core.orchestrator import Pipeline, PipelineResult, TaskBoard
from backend.core.orchestrator.board import DONE, FAILED, StageRecord
from backend.core.runtime import Runtime, new_run_id
from backend.core.workspace import slugify

logger = logging.getLogger(__name__)

#: How many finished runs stay in memory. The events and boards are on disk, so
#: this is a UI convenience window, not the history of record.
MAX_TRACKED_RUNS = 200


class RunNotFound(LookupError):
    """No run with that id exists (404)."""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"No run with id '{run_id}'")
        self.run_id = run_id


class RunBusyError(RuntimeError):
    """Another run holds the active slot (409)."""

    def __init__(self, active_run_id: str) -> None:
        super().__init__(f"Run '{active_run_id}' is active; cancel it before starting another")
        self.active_run_id = active_run_id


class GateNotWaiting(RuntimeError):
    """Approve/reject on a run that is not paused at a gate (409)."""

    def __init__(self, run_id: str, status: str) -> None:
        super().__init__(
            f"Run '{run_id}' is not waiting for approval (status: '{status}')"
        )
        self.run_id = run_id
        self.status = status


@dataclass
class GateState:
    """One approval gate: what is being approved, and the human's answer.

    ``event`` is the single wake-up channel: approve, reject, cancel and
    shutdown all set it, so the worker thread blocked in ``Event.wait()`` wakes
    the moment any of them happens — never on a poll or a timeout (D21).
    """

    gate: str
    payload: dict[str, Any] = field(default_factory=dict)
    opened_at: str = field(default_factory=utc_now)
    decision: str | None = None  # "approved" | "rejected" | None (still waiting)
    note: str | None = None
    decided_at: str | None = None
    event: threading.Event = field(default_factory=threading.Event)

    def to_dict(self) -> dict[str, Any]:
        """The gate as the API returns it (never the Event itself)."""
        return {
            "gate": self.gate,
            "payload": dict(self.payload),
            "opened_at": self.opened_at,
            "decision": self.decision,
            "note": self.note,
            "decided_at": self.decided_at,
        }


@dataclass
class RunRecord:
    """Everything the API knows about one run, in memory.

    The task board on disk and the events table remain the durable record; this
    is the live view the endpoints read. All mutation happens under
    :attr:`RunManager._lock`.
    """

    run_id: str
    request: str
    project: str
    options: RunOptions
    status: RunStatus = "queued"
    created_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    ok: bool | None = None
    reason: str | None = None
    result: PipelineResult | None = None
    calls: int = 0
    tokens: int = 0
    #: Where this run's board actually lives. The pipeline decides (it uses its
    #: own runtime's root), so the API records the real path instead of guessing
    #: one from its own settings — otherwise a run with a different root would
    #: report an empty board.
    board_path: str = ""
    #: This run's own project folder, recorded for the same reason as
    #: board_path. The file viewer reads only inside it (D30).
    project_dir: str = ""
    #: set by POST /cancel; the pipeline polls it at its own checkpoints
    cancel_requested: threading.Event = field(default_factory=threading.Event)
    #: the current (or last) approval gate this run paused at; None when the
    #: run never asked for approval (D21)
    gate: GateState | None = None

    @property
    def stages(self) -> list[str]:
        return list(self.options.stages)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def to_summary(self) -> RunSummary:
        return RunSummary(
            run_id=self.run_id,
            request=self.request,
            project=self.project,
            status=self.status,
            stages=self.stages,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            error=self.error,
            ok=self.ok,
            reason=self.reason,
        )

    def to_detail(
        self, board: TaskBoard | None, *, queue_position: int | None = None
    ) -> RunStateResponse:
        """Assemble the detail response: record + board + result, never a re-run."""
        return RunStateResponse(
            run_id=self.run_id,
            request=self.request,
            project=self.project,
            status=self.status,
            # The request's own vocabulary (--no-run-tests), not the internal one.
            options=self.options.to_response(),
            stages=self.stages,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            error=self.error,
            board=board.to_dict() if board is not None else None,
            agents=_agent_outputs(board),
            stage_states=_stage_states(board, self.stages),
            files=_manifest_files(board, self.result),
            tests=(board.last_execution if board is not None else None)
            or (self.result.tests if self.result is not None else None),
            budget={
                "calls": self.calls,
                "tokens": self.tokens,
                "last_seq": -1,
            },
            result=self.result.to_dict() if self.result is not None else None,
            queue_position=queue_position,
            gate=self.gate.to_dict() if self.gate is not None else None,
        )



class RunManager:
    """Owns the API's runs: one active, always inspectable, cancellable.

    A runtime *factory* is injected rather than a runtime instance: each run needs
    its own bus (its own run id), budget and router, and tests inject a factory
    that builds an offline mock runtime so no quota is ever spent.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        event_store: Any,
        runtime_factory: Any | None = None,
    ) -> None:
        self.settings = settings
        self.event_store = event_store
        self._runtime_factory = runtime_factory or self._default_runtime
        self._runs: dict[str, RunRecord] = {}
        self._order: list[str] = []
        #: run ids waiting for the single worker, oldest first
        self._queue: list[str] = []
        self._active_id: str | None = None
        self._lock = threading.RLock()
        #: the pipeline objects, so a cancel can reach a running command
        self._pipelines: dict[str, Pipeline] = {}
        #: exactly one worker thread: the queue behind it is what serialises runs
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="sda-run"
        )
        self._closed = False
        self._default_stages: list[str] | None = None

    # -- runtime construction ------------------------------------------------
    def _default_runtime(self, run_id: str) -> Runtime:
        """A real runtime for this run: real config, real providers, real bus."""
        return Runtime.create(root=self.settings.root, run_id=run_id, echo=True)

    # -- introspection -------------------------------------------------------
    @property
    def active_run_id(self) -> str | None:
        with self._lock:
            return self._active_id

    def get(self, run_id: str) -> RunRecord:
        with self._lock:
            record = self._runs.get(run_id)
        if record is None:
            raise RunNotFound(run_id)
        return record

    def is_finished(self, run_id: str) -> bool | None:
        """``True``/``False`` for a run this process manages, ``None`` otherwise.

        Read under the lock: a ``True`` here means the closing event is already
        persisted (see :meth:`_finish_locked`).
        """
        with self._lock:
            record = self._runs.get(run_id)
            return None if record is None else record.is_terminal

    def list(self, *, limit: int = 50) -> list[RunRecord]:
        """Newest first, bounded so the in-memory history cannot grow forever."""
        with self._lock:
            ids = self._order[::-1][: max(1, min(int(limit), MAX_TRACKED_RUNS))]
            return [self._runs[rid] for rid in ids]

    def count(self) -> int:
        """Total runs tracked, ignoring any list ``limit``."""
        with self._lock:
            return len(self._order)

    def detail(self, run_id: str) -> RunStateResponse:
        """The full run state for ``GET /api/runs/{id}``.

        Re-reads the board from disk on every call rather than caching it: a run
        in flight writes that file continuously, and a cached copy would show a
        stale stage list in exactly the situation the UI is watching.
        """
        record = self.get(run_id)
        state = record.to_detail(
            self.board(run_id), queue_position=self._queue_position(run_id)
        )
        # Always report the cursor, finished or not: the UI uses it as the
        # ``after_seq`` it passes to the stream when it reconnects.
        state.last_seq = self.event_store.last_seq(run_id)
        return state

    def _queue_position(self, run_id: str) -> int | None:
        """1-based place in the waiting queue, or ``None`` when not waiting.

        Computed from the live queue rather than stored on the record: a position
        that is written once and never updated is worse than none.
        """
        with self._lock:
            if self._active_id == run_id or self._runs[run_id].status != "queued":
                return None
            try:
                return self._queue.index(run_id) + 1
            except ValueError:  # pragma: no cover - races with a worker starting
                return None

    def project_dir(self, run_id: str) -> Path | None:
        """The run's project folder, or None before its pipeline has started."""
        record = self.get(run_id)
        return Path(record.project_dir) if record.project_dir else None

    def board(self, run_id: str) -> TaskBoard | None:
        """Load the run's task board from disk, if the pipeline has written one.

        The path comes from the run itself (the pipeline recorded it), because a
        runtime may be rooted somewhere other than the API's own settings.
        """
        record = self.get(run_id)
        path = Path(record.board_path) if record.board_path else (
            self.settings.runs_dir / run_id / "board.json"
        )
        if not path.exists():
            return None
        try:
            return TaskBoard.load(path)
        except Exception:  # noqa: BLE001 - a half-written board must not 500 the UI
            logger.warning("could not read board for run %s", run_id, exc_info=True)
            return None

    def default_stages(self) -> list[str]:
        """The pipeline order a run gets when the request names no subset.

        Read from the registry (``config/agents.yaml``) so the API offers exactly
        the stages the CLI would run, and cached because it cannot change while
        the process lives.
        """
        with self._lock:
            if self._default_stages is not None:
                return list(self._default_stages)
        try:
            from backend.core.provider.registry import Registry

            stages = list(Registry.load(self.settings).pipeline.stages)
        except Exception:  # noqa: BLE001 - config problems surface per run, not here
            logger.warning("could not read pipeline stages from config", exc_info=True)
            from backend.core.orchestrator.pipeline import STAGE_SPECS

            stages = list(STAGE_SPECS)
        with self._lock:
            self._default_stages = list(stages)
        return list(stages)

    def workspace_projects(self) -> list[dict[str, Any]]:
        """One entry per live project: runs we created plus folders on disk.

        A folder appears the moment its run is created — before the pipeline has
        written anything — so the UI can show the project as queued rather than
        missing. Folders on disk with no run yet (copied in by hand, left from
        before a restart) appear too. Newest creation first.
        """
        entries: dict[str, dict[str, Any]] = {}
        with self._lock:
            for run_id in reversed(self._order):
                record = self._runs[run_id]
                entries.setdefault(
                    record.project,
                    {
                        "project": record.project,
                        "file_count": 0,
                        "created_at": record.created_at,
                    },
                )
        root = self.settings.workspace_dir
        created: dict[str, str] = {}
        mtimes: dict[str, float] = {}
        if root.exists():
            for folder in root.iterdir():
                if not folder.is_dir():
                    continue
                try:
                    mtimes[folder.name] = folder.stat().st_mtime
                except OSError:
                    continue
                created[folder.name] = _iso_from_mtime(folder)
        for name in set(created) | set(mtimes):
            entries.setdefault(
                name,
                {
                    "project": name,
                    "file_count": 0,
                    "created_at": created.get(name),
                },
            )
        for name, entry in entries.items():
            folder = root / name
            if folder.is_dir():
                entry["file_count"] = sum(
                    1 for p in folder.rglob("*") if p.is_file()
                )
                entry["created_at"] = created.get(name, entry.get("created_at"))
            elif entry.get("created_at") is None:
                entry["created_at"] = _iso_from_mtime(root)
        return sorted(
            entries.values(),
            key=lambda e: (
                _parse_ts(e.get("created_at")),
                mtimes.get(str(e.get("project")), float("-inf")),
                str(e.get("project")),
            ),
            reverse=True,
        )

    # -- create --------------------------------------------------------------
    def create(
        self,
        *,
        request: str,
        options: RunOptions,
        project: str | None = None,
        run_id: str | None = None,
    ) -> RunRecord:
        """Register a run, append it to the queue, and schedule its worker.

        Every accepted run is ``202``: the single worker behind the queue is what
        serialises execution, and a refused request would make the UI the only
        place a backlog could exist. A run cancelled before its turn starts never
        runs at all (see :meth:`cancel`).
        """
        run_id = run_id or new_run_id()
        record = RunRecord(
            run_id=run_id,
            request=request,
            project=slugify(project or request),
            options=options,
        )
        with self._lock:
            if self._closed:
                raise RuntimeError("run manager is closed")
            self._runs[run_id] = record
            self._order.append(run_id)
            self._queue.append(run_id)
            self._trim_locked()
            self._emit(
                run_id,
                "api.run_queued",
                f"queued: {request[:120]}",
                stages=list(options.stages),
                project=record.project,
            )
        self._executor.submit(self._run_worker, record)
        return record

    def _trim_locked(self) -> None:
        """Drop the oldest *finished* runs once the window is full."""
        while len(self._order) > MAX_TRACKED_RUNS:
            for index, run_id in enumerate(self._order):
                if self._runs[run_id].is_terminal:
                    del self._order[index]
                    self._runs.pop(run_id, None)
                    self._pipelines.pop(run_id, None)
                    break
            else:  # every tracked run is still active: nothing may be dropped
                return

    def cancel(self, run_id: str) -> tuple[bool, str]:
        """Request cancellation. Returns ``(accepted, note)``.

        Two cases, and the difference matters to the UI:

        * a **queued** run never started, so it is marked ``cancelled`` right now
          and dropped from the queue — its worker returns immediately without
          building a runtime or calling a provider;
        * a **running** run sets the flag and the pipeline stops at its next
          checkpoint — or immediately, mid-command, because the runner's cancel
          hook kills that command's whole process tree.

        Either way the terminal status of a running run is set by the worker, not
        here: this call never blocks on a provider request that cannot be torn
        out from under the router.
        """
        record = self.get(run_id)
        with self._lock:
            # checked under the lock: a run finishing right now must not be
            # turned back into "cancelling"
            if record.is_terminal:
                return False, f"run already finished with status '{record.status}'"
            record.cancel_requested.set()
            self._emit(
                run_id,
                "api.run_cancel_requested",
                "cancellation requested; stopping at the next checkpoint",
            )
            if record.gate is not None:
                # Wake a run blocked at an approval gate right now: the same
                # event approve/reject would set, never a poll (D21).
                record.gate.event.set()
            if record.status == "queued":
                # Not started: take it out of the queue and finish it here.
                if run_id in self._queue:
                    self._queue.remove(run_id)
                self._finish_locked(record, "cancelled", ok=False, reason="cancelled")
                note = "cancelled before the run started"
            else:
                record.status = "cancelling"
                note = "cancellation requested"
        return True, note

    # -- approval gates (Part A, step 3, D21) --------------------------------
    def approve(self, run_id: str, note: str | None = None) -> GateState:
        """Record an approval and wake the waiting worker.

        Raises :class:`GateNotWaiting` (409) when the run is not paused at an
        undecided gate — approving a queued, running or finished run is a
        client bug, not something to silently ignore.
        """
        return self._decide(run_id, "approved", note)

    def reject(self, run_id: str, note: str | None = None) -> GateState:
        """Record a rejection and wake the waiting worker, ending the run."""
        return self._decide(run_id, "rejected", note)

    def _decide(self, run_id: str, decision: str, note: str | None) -> GateState:
        """Resolve the open gate. Every check runs under the lock, so a
        decision can never race a cancel or a second decision."""
        record = self.get(run_id)
        with self._lock:
            state = record.gate
            if (
                state is None
                or state.decision is not None
                or record.status != "waiting_approval"
                or record.cancel_requested.is_set()
            ):
                raise GateNotWaiting(run_id, record.status)
            state.decision = decision
            state.note = note
            state.decided_at = utc_now()
            # recorded before the worker wakes, so it can never land after the
            # run's closing event
            detail = f"{state.gate} {decision}" + (f": {note}" if note else "")
            self._emit(run_id, f"api.run_{decision}", detail, gate=state.gate, note=note)
            # the single wake-up: the worker's Event.wait() returns right now
            state.event.set()
        return state

    def _approval_hook(self, record: RunRecord, gate: str, payload: dict) -> None:
        """Pause the worker thread at an approval gate (``Pipeline`` calls this).

        Runs on the worker thread inside ``Pipeline.run``, so the board and the
        event log stay the pipeline's own. The wait is a bare ``Event.wait()``
        with no timeout: approve, reject, cancel and shutdown all set that
        event, so the gate wakes the instant any of them happens. A cancel wins
        over a decision that arrives with it, and a gate woken without any
        decision (shutdown) is treated as a cancel — a gate must never fail
        open.
        """
        with self._lock:
            if record.cancel_requested.is_set():
                raise RunCancelled(record.run_id, gate)
            state = GateState(gate=gate, payload=dict(payload))
            record.gate = state
            record.status = "waiting_approval"
        self._emit(
            record.run_id,
            "api.run_waiting_approval",
            f"waiting for approval: {gate}",
            gate=gate,
            **payload,
        )
        state.event.wait()

        if record.cancel_requested.is_set():
            raise RunCancelled(record.run_id, gate)
        if state.decision == "approved":
            with self._lock:
                if record.status == "waiting_approval":
                    record.status = "running"
            return
        if state.decision == "rejected":
            raise ApprovalRejected(gate, note=state.note or "")
        raise RunCancelled(record.run_id, gate)

    # -- the worker ----------------------------------------------------------
    def _run_worker(self, record: RunRecord) -> None:
        """Run one pipeline to completion, then release the slot. Never raises.

        Everything that can go wrong is turned into a terminal status here, and
        :meth:`_release` runs from the ``finally`` on every path — success,
        failure, cancellation, or a broken runtime factory. That is the whole
        point of this method: the active slot must never outlive the run.
        """
        runtime: Runtime | None = None
        try:
            with self._lock:
                if record.run_id in self._queue:
                    self._queue.remove(record.run_id)
                if record.is_terminal:
                    # Cancelled while it waited: never start it.
                    return
                self._active_id = record.run_id
                if record.cancel_requested.is_set():
                    self._finish_locked(
                        record, "cancelled", ok=False, reason="cancelled"
                    )
                    return
                record.status = "running"
                record.started_at = utc_now()
            self._emit(
                record.run_id,
                "api.run_started",
                f"starting: {record.request[:120]}",
                project=record.project,
            )
            runtime = self._runtime_factory(record.run_id)
            # Subscribe before the pipeline can emit, so no event is missed.
            self.event_store.subscribe(runtime.bus)
            options = record.options
            pipeline = Pipeline(
                runtime,
                stages=list(options.stages),
                override=_single_candidate(runtime, options.provider, options.model),
                agent_overrides=_agent_overrides(runtime, options.override),
                project=record.project,
                apply_workspace=options.apply_workspace,
                run_tests=options.run_tests,
                dry_run=options.dry_run,
                cancel_check=record.cancel_requested.is_set,
                # Part A step 3: the gates this run asked for, and the blocking
                # pause point that parks the worker until a human decides (D21)
                approval_gates=list(options.approval_gates),
                approval_hook=partial(self._approval_hook, record),
            )
            with self._lock:
                self._pipelines[record.run_id] = pipeline
                record.board_path = str(pipeline.board_path)
                record.project_dir = str(
                    runtime.settings.workspace_dir / slugify(record.project)
                )
            self._apply_result(record, pipeline.run(record.request))
        except RunCancelled as exc:
            self._finish(record, "cancelled", error=str(exc), reason="cancelled")
        except AgentSystemError as exc:
            # Config/budget/provider: the pipeline already recorded the failed
            # stage and emitted its own "error" event; record the terminal state.
            self._finish(record, "failed", error=str(exc), reason="failed")
        except Exception as exc:  # noqa: BLE001 - a worker must never kill the pool
            logger.exception("run %s crashed", record.run_id)
            self._finish(record, "failed", error=f"internal error: {exc}", reason="failed")
        finally:
            if runtime is not None:
                try:
                    runtime.close()
                except Exception:  # noqa: BLE001 - closing must not mask the result
                    logger.warning("closing runtime for %s failed", record.run_id)
            with self._lock:
                self._pipelines.pop(record.run_id, None)
            # The active slot is released last and unconditionally, so a run that
            # failed, was cancelled, or crashed while building its runtime cannot
            # leave the API permanently "busy".
            self._release(record)

    def _apply_result(self, record: RunRecord, result: PipelineResult) -> None:
        """Turn a finished :class:`PipelineResult` into a terminal status."""
        record.result = result
        record.calls = int(result.budget.get("calls", 0))
        record.tokens = int(result.budget.get("tokens", 0))
        if result.reason == "cancelled" or record.cancel_requested.is_set():
            self._finish(record, "cancelled", ok=False, reason="cancelled")
        elif result.reason == "rejected":
            # A human said no at a gate: the gate itself carries the exact
            # wording (which gate, their note) into the run's error text.
            state = record.gate
            where = f"the '{state.gate}'" if state is not None else "an"
            note = f": {state.note}" if state is not None and state.note else ""
            self._finish(
                record,
                "failed",
                ok=False,
                reason="rejected",
                error=f"Run rejected at {where} approval gate{note}",
            )
        elif result.ok:
            self._finish(record, "succeeded", ok=True, reason=result.reason)
        else:
            self._finish(
                record,
                "failed",
                ok=False,
                reason=result.reason,
                error=_reason_to_error(result),
            )

    def _finish(
        self,
        record: RunRecord,
        status: RunStatus,
        *,
        ok: bool | None = None,
        reason: str | None = None,
        error: str | None = None,
    ) -> None:
        """Set the terminal status and emit the closing event, once."""
        with self._lock:
            self._finish_locked(record, status, ok=ok, reason=reason, error=error)

    def _finish_locked(
        self,
        record: RunRecord,
        status: RunStatus,
        *,
        ok: bool | None = None,
        reason: str | None = None,
        error: str | None = None,
    ) -> bool:
        """Set the terminal fields and emit the closing event, under the lock.

        ``False`` if already terminal. Every path to a terminal status comes
        through here, so every run gets exactly one ``api.run_<status>`` event,
        written in the same critical section that sets the status. That event is
        the last one a run ever has, which is what lets the SSE stream end on it.
        """
        if record.is_terminal:
            return False
        record.status = status
        record.ok = ok
        record.reason = reason
        record.error = error
        record.finished_at = utc_now()
        self._emit(
            record.run_id,
            f"api.run_{status}",
            error or f"run {status}",
            ok=ok,
            reason=reason,
            calls=record.calls,
            tokens=record.tokens,
        )
        return True

    def _release(self, record: RunRecord) -> None:
        """Release the worker slot. Safe to call more than once.

        Called from the worker's ``finally`` and from :meth:`shutdown`, which is
        why it checks ownership instead of assuming it: a shutdown during a run
        must not have the slot re-taken by a late worker exit.
        """
        with self._lock:
            if self._active_id == record.run_id:
                self._active_id = None
            if record.run_id in self._queue:
                self._queue.remove(record.run_id)
            if not record.is_terminal:
                # The worker exited without a terminal status (only reachable on
                # a broken factory): never leave a run looking unfinished.
                self._finish_locked(
                    record, "cancelled", ok=False, reason="cancelled"
                )

    # -- events --------------------------------------------------------------
    def _emit(self, run_id: str, kind: str, message: str, **data: Any) -> None:
        """Emit an API-level event through the same store the pipeline uses.

        Callers on a request thread (create, cancel, approve/reject) and every
        terminal transition hold ``self._lock`` while emitting, so no event can
        be persisted after a run's closing event. The store never takes this
        lock, so there is no lock-order cycle; the write is one SQLite row.
        """
        try:
            self.event_store.sink(
                Event(kind=kind, message=message, run_id=run_id, data=data)
            )
        except Exception:  # noqa: BLE001 - an event write must never fail a run
            logger.warning("could not persist %s event", kind, exc_info=True)

    # -- shutdown ------------------------------------------------------------
    def shutdown(self, *, wait: bool = True, timeout: float = 5.0) -> None:
        """Cancel in-flight work and stop accepting new runs.

        Called from the app's lifespan shutdown. A run mid provider-call is asked
        to stop at its next checkpoint; a run mid command has its whole process
        tree killed by the runner's cancel hook. The wait is bounded because the
        user asked to close the app, not to finish the build.
        """
        with self._lock:
            self._closed = True
            records = [r for r in self._runs.values() if not r.is_terminal]
        for record in records:
            record.cancel_requested.set()
            if record.gate is not None:
                # never leave a worker parked at a gate during shutdown
                record.gate.event.set()
        if wait and records:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and any(
                not r.is_terminal for r in records
            ):
                time.sleep(0.05)
        self._executor.shutdown(wait=False, cancel_futures=True)
        for record in records:
            self._release(record)

# -- module helpers -----------------------------------------------------------
def _agent_overrides(
    runtime: Runtime, rows: list[list[str]] | None
) -> dict[str, list[tuple[Any, Any]]] | None:
    """Resolve ``[[agent, provider, model], …]`` into per-agent routing chains.

    Unknown providers or models raise :class:`ConfigError`, which the worker turns
    into a ``failed`` run with the message — not a 500 from the worker thread.
    """
    if not rows:
        return None
    chains: dict[str, list[tuple[Any, Any]]] = {}
    for agent_name, provider_name, model_id in rows:
        spec = runtime.registry.provider(provider_name)
        chains[agent_name] = [(spec, spec.model(model_id))]
    return chains


def _iso_from_mtime(folder: Path) -> str:
    """Folder creation time in the same shape :func:`utc_now` returns."""
    from datetime import datetime

    try:
        stamp = folder.stat().st_mtime
    except OSError:  # pragma: no cover - folder vanished mid-listing
        stamp = time.time()
    return datetime.fromtimestamp(stamp, UTC).isoformat(timespec="seconds")


def _parse_ts(value: Any) -> str:
    """Sortable creation stamp; missing stamps sort before every real one."""
    return str(value) if value else ""


def _agent_outputs(board: TaskBoard | None) -> list[AgentOutput]:
    """One :class:`AgentOutput` per stage that has produced anything.

    Reading the board (not re-running an agent) is what makes this endpoint
    free: the expensive part already happened during the run.
    """
    if board is None:
        return []
    outputs: list[AgentOutput] = []
    for stage in board.order:
        record = board.records.get(stage)
        if record is None or (record.status not in (DONE, FAILED) and not record.text):
            continue
        outputs.append(
            AgentOutput(
                agent=record.agent or stage,
                stage=stage,
                status=record.status,
                output=record.text,
                files=_declared_files(record),
                run_command=_run_command(record),
                verdict=_verdict(record),
                notes=list(record.notes),
                error=record.error or None,
                started_at=record.started_at or None,
                finished_at=record.finished_at or None,
                duration_ms=record.latency_ms or None,
                tokens={"total": record.tokens},
                target=record.target,
                attempts=record.attempts,
            )
        )
    return outputs


def _declared_files(record: StageRecord) -> list[str]:
    """File paths a stage declared in its parsed JSON output, if any."""
    parsed = record.parsed if isinstance(record.parsed, dict) else {}
    files = parsed.get("files")
    if not isinstance(files, list):
        return []
    return [str(item.get("path", "")) for item in files if isinstance(item, dict)]


def _run_command(record: StageRecord) -> str | None:
    """The tester's command, the one the pipeline actually executed."""
    parsed = record.parsed if isinstance(record.parsed, dict) else {}
    command = parsed.get("run_command")
    return str(command) if command else None


def _verdict(record: StageRecord) -> str | None:
    parsed = record.parsed if isinstance(record.parsed, dict) else {}
    verdict = parsed.get("verdict")
    return str(verdict) if verdict else None


def _stage_states(board: TaskBoard | None, stages: list[str]) -> list[StageState]:
    """Every stage in the run's plan, including ones still pending."""
    if board is None:
        return [
            StageState(stage=stage, agent="", status="pending") for stage in stages
        ]
    states: list[StageState] = []
    for stage in stages:
        record = board.records.get(stage)
        if record is None:
            states.append(StageState(stage=stage, agent="", status="pending"))
            continue
        states.append(
            StageState(
                stage=stage,
                agent=record.agent,
                status=record.status,
                error=record.error or None,
                output=record.text or None,
            )
        )
    return states


def _manifest_files(
    board: TaskBoard | None, result: PipelineResult | None
) -> list[WrittenFile]:
    """Files the run wrote, from the board's manifest, capped and path-safe."""
    if board is None:
        return []
    files: list[WrittenFile] = []
    for stage in board.order:
        record = board.records.get(stage)
        if record is None:
            continue
        parsed = record.parsed if isinstance(record.parsed, dict) else {}
        manifest = parsed.get("files")
        if not isinstance(manifest, list):
            continue
        for item in manifest:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "")
            if not path:
                continue
            content = item.get("content")
            size = int(item.get("size") or (len(content) if isinstance(content, str) else 0))
            files.append(
                WrittenFile(
                    path=path,
                    size=size,
                    status="written" if result is not None and result.ok else "planned",
                    note=str(item.get("note") or "") or None,
                )
            )
            if len(files) >= MAX_FILES_IN_RESPONSE:
                return files
    return files


def _single_candidate(
    runtime: Runtime, provider: str | None, model: str | None
) -> list[tuple[Any, Any]] | None:
    """Build a one-entry routing chain from the request's provider/model.

    Same rule as the CLI's ``--provider``/``--model``: no provider means "use the
    agent's configured chain", and no model picks the provider's first model.
    """
    if not provider:
        return None
    spec = runtime.registry.provider(provider)
    if not model:
        if not spec.models:
            from backend.core.errors import ConfigError

            raise ConfigError(f"Provider '{provider}' has no models configured")
        model = sorted(spec.models)[0]
    return [(spec, spec.model(model))]


def _reason_to_error(result: PipelineResult) -> str | None:
    """A human sentence for a run that ended without raising."""
    if result.reason == "review":
        return "Reviewer requested changes and the fix loop was exhausted."
    if result.reason == "tests":
        execution = result.tests or {}
        command = execution.get("command", "the test command")
        return f"Tests were still failing after the fix loop ({command})."
    if result.reason == "cancelled":
        return "Cancelled by the user."
    return None

"""The orchestrator: runs specialist stages in order over a shared task board.

Flow for a triaged **small** task (a script, a calculator - see ``triage.py``)::

    coder -> auto-checks -> done

Flow for **medium/large** tasks (stage order comes from ``pipeline:`` in
config/agents.yaml)::

    planner -> architect -> coder -> tester
        -> auto-checks (py_compile, ruff, pytest; exact error back to the
           coder as a minimal patch, up to max_autocheck_iterations)
        -> reviewer
        --(a BLOCKER: coder fixes, reviewer re-checks, up to
            max_review_iterations)-->
    devops -> docs

Either loop running out of tries does not just say "failed": it raises
:class:`PipelineEscalated` with the current code and the exact error attached,
so a human has something to act on. The same repeated error twice in a row
switches the coder to the next model in its routing chain before trying again
(a different model breaks a same-model deadlock better than retrying it).

Design rules this module keeps:

  * The agent class does not know it is part of a pipeline; the pipeline tells
    each agent what to do and stores the result on the board.
  * Stage order, the loop limits and every model choice live in YAML, not
    here - changing the process is a config edit (D6).
  * Every stage transition goes through the event bus, so the CLI shows the
    run live and Phase 4's UI gets the same events over a WebSocket.
  * Any :class:`AgentSystemError` (budget, provider, config) stops the run:
    the failing stage is marked, the rest are skipped, the board is saved, and
    the error is re-raised for the CLI to map to an exit code.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from backend.core.errors import (
    AgentSystemError,
    AllProvidersFailed,
    ApprovalRejected,
    ConfigError,
    PipelineEscalated,
    RunCancelled,
)
from backend.core.orchestrator import triage
from backend.core.orchestrator.board import (
    BOARD_CANCELLED,
    BOARD_FAILED,
    BOARD_RUNNING,
    BOARD_SUCCEEDED,
    PENDING,
    TaskBoard,
)
from backend.core.workspace import (
    FILE_STAGES,
    ApplyReport,
    CommandNotAllowed,
    CommandResult,
    CommandRunner,
    Workspace,
    apply_board,
    slugify,
    test_command_for,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a runtime cycle
    from backend.core.runtime import Runtime

Candidate = tuple[Any, Any]


@dataclass(frozen=True)
class StageSpec:
    """What one stage is: which agent runs it, what it needs, what to ask.

    ``instruction`` is the user message sent to the agent; when it is empty
    (planner) the goal itself is the message. ``needs`` names whose completed
    board artifacts become this stage's CONTEXT, and ``wants_execution`` adds
    the latest command output from the workspace as ``### execution``.
    """

    agent: str
    instruction: str
    needs: tuple[str, ...] = ()
    #: set for stages that can be re-run with a hand-built fix context (coder)
    fix_instruction: str | None = None
    #: Phase 3: show the workspace's last command output in this stage's CONTEXT
    wants_execution: bool = False


STAGE_SPECS: dict[str, StageSpec] = {
    "planner": StageSpec(agent="planner", instruction=""),
    "architect": StageSpec(
        agent="architect",
        instruction="Produce the system design for this request.",
        needs=("planner",),
    ),
    "coder": StageSpec(
        agent="coder",
        instruction=(
            "Implement every file needed for this request. Output only the JSON "
            "file manifest - every file you emit is written into the project folder."
        ),
        needs=("planner", "architect"),
        fix_instruction=(
            "Fix mode. CONTEXT has the current code (### code), the exact error "
            "(### error) and a one-line history of prior attempts (### history) - "
            "do not repeat a fix that already failed the same way. Make the "
            "SMALLEST patch that resolves the error, then re-emit the JSON file "
            "manifest with ONLY the files you changed, never the whole project."
        ),
    ),
    "tester": StageSpec(
        agent="tester",
        instruction=(
            "Write the test suite for the implementation in CONTEXT. The command "
            "you name in run_command is actually executed in the project folder, "
            "so it must work as written on Windows PowerShell."
        ),
        needs=("architect", "coder"),
    ),
    "reviewer": StageSpec(
        agent="reviewer",
        instruction=(
            "Review the implementation in CONTEXT against the design and tests. "
            "If ### execution is present, judge the real command output: failing "
            "evidence beats any claim in a summary. Output only the verdict JSON."
        ),
        needs=("architect", "coder", "tester"),
        wants_execution=True,
    ),
    "devops": StageSpec(
        agent="devops",
        instruction="Produce the run/setup configuration and files for this project.",
        needs=("planner", "coder"),
    ),
    "docs": StageSpec(
        agent="docs",
        instruction="Produce the README and documentation files for this project.",
        needs=("planner", "architect", "coder", "reviewer"),
    ),
}

#: verdict strings the reviewer prompt is allowed to produce, and how they map
#: onto pipeline behaviour. Liberal on purpose: free models drift in wording.
_APPROVE = {"approve", "approved", "lgtm"}
_REJECT = {"reject", "rejected", "changes_requested", "changes requested", "revise"}

#: The approval gates a run may request (Phase 4, D21). ``plan`` and
#: ``architecture`` open when their stage completes; ``execution`` opens before
#: every command the pipeline is about to run.
APPROVAL_GATES: tuple[str, ...] = ("plan", "architecture", "execution")

#: gate name -> the stage whose completion opens it
GATE_AFTER_STAGE: dict[str, str] = {"plan": "planner", "architecture": "architect"}
#: stage -> gate, the form the run loop looks up
_STAGE_GATES: dict[str, str] = {stage: gate for gate, stage in GATE_AFTER_STAGE.items()}

#: deterministic checks that run before pytest, cheapest/most-diagnostic first
_PRE_TEST_CHECKS: tuple[tuple[str, str], ...] = (
    ("compile", "python -m compileall -q ."),
    ("lint", "python -m ruff check ."),
)


def _signature(text: str) -> str:
    """A short fingerprint of an error, for "is this the same failure again?"."""
    return hashlib.sha1(" ".join((text or "").split()).lower().encode("utf-8")).hexdigest()


@dataclass
class PipelineResult:
    """Everything the CLI/UI needs after a pipeline run."""

    run_id: str
    goal: str
    ok: bool
    #: "ok" | "review" | "cancelled" | "rejected" | "escalated"
    reason: str
    stages: list[str]
    verdict: str | None
    board_path: str
    events_file: str = ""
    budget: dict[str, int] = field(default_factory=dict)
    #: Phase 3 - where the code went and what running it said
    project: str = ""
    workspace: str = ""
    files: dict[str, int] = field(default_factory=dict)
    tests: dict[str, Any] | None = None
    #: triage's classification: "small" | "medium" | "large" | "custom"
    size: str = ""
    #: set only when reason == "escalated": {"stage", "reason", "error", "code"}
    escalation: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "goal": self.goal,
            "ok": self.ok,
            "reason": self.reason,
            "stages": list(self.stages),
            "verdict": self.verdict,
            "board_path": self.board_path,
            "events_file": self.events_file,
            "budget": dict(self.budget),
            "project": self.project,
            "workspace": self.workspace,
            "files": dict(self.files),
            "tests": dict(self.tests) if self.tests else None,
            "size": self.size,
            "escalation": dict(self.escalation) if self.escalation else None,
        }


class Pipeline:
    """Executes a stage sequence against a runtime, recording everything."""

    def __init__(
        self,
        runtime: Runtime,
        *,
        stages: list[str] | None = None,
        triage: bool | None = None,
        max_autocheck_iterations: int | None = None,
        max_review_iterations: int | None = None,
        board_path: Path | str | None = None,
        override: list[Candidate] | None = None,
        agent_overrides: dict[str, list[Candidate]] | None = None,
        project: str | None = None,
        apply_workspace: bool | None = None,
        run_tests: bool | None = None,
        dry_run: bool = False,
        cancel_check: Callable[[], bool] | None = None,
        approval_gates: list[str] | None = None,
        approval_hook: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.runtime = runtime
        config = runtime.registry.pipeline
        #: explicit stages (CLI --stages, the API request, or a test) bypass
        #: triage entirely and run exactly what was asked for
        self._stage_override = list(stages) if stages is not None else None
        self._default_stages = list(config.stages)
        self.triage_enabled = config.triage if triage is None else triage
        self.max_autocheck_iterations = (
            config.max_autocheck_iterations
            if max_autocheck_iterations is None
            else max_autocheck_iterations
        )
        self.max_review_iterations = (
            config.max_review_iterations if max_review_iterations is None else max_review_iterations
        )
        if self.max_autocheck_iterations < 0:
            raise ConfigError("pipeline.max_autocheck_iterations must be >= 0")
        if self.max_review_iterations < 0:
            raise ConfigError("pipeline.max_review_iterations must be >= 0")
        #: resolved lazily in run() (triage needs the goal text); validated now
        #: against whatever we know already so a bad config fails fast
        self.stages = list(self._stage_override or self._default_stages)
        self.task_size = "custom" if self._stage_override is not None else ""
        self._validate_stages(self.stages)
        self.override = override
        #: Phase 4: per-agent routing from the API request
        #: (``[["coder", "groq", "llama"], …]``). A per-agent entry wins over the
        #: run-wide ``override`` chain, which is what the CLI's --provider means.
        self.agent_overrides = dict(agent_overrides or {})
        self.board_path = (
            Path(board_path)
            if board_path
            else runtime.settings.runs_dir / runtime.run_id / "board.json"
        )

        # -- Phase 3: workspace execution ----------------------------------
        self.project_name = project
        self.apply_workspace = (
            config.apply_workspace if apply_workspace is None else apply_workspace
        )
        self.run_tests = config.run_tests if run_tests is None else run_tests
        self.dry_run = dry_run
        self.execution = runtime.registry.limits.execution
        self.workspace = Workspace(runtime.settings.workspace_dir)
        #: Phase 4: the API's cancel flag. Checked between stages and polled by the
        #: command runner, which kills the process *tree* when it turns true (D20).
        self.cancel_check = cancel_check
        self.runner = CommandRunner(self.execution, cancel_check=cancel_check)

        # -- Phase 4: approval gates (D21) ----------------------------------
        #: gates this run must pause at; empty for the CLI
        gates = list(approval_gates or [])
        for name in gates:
            if name not in APPROVAL_GATES:
                raise ConfigError(
                    f"Unknown approval gate '{name}'. Known gates: "
                    f"{', '.join(APPROVAL_GATES)}"
                )
        self.approval_gates = set(gates)
        #: blocking pause point injected by the API's run manager; ``None`` for
        #: the CLI, which never asks a human and therefore never pauses
        self.approval_hook = approval_hook
        self._project = slugify(project) if project else ""
        self._last_apply: ApplyReport | None = None

    # -- pieces -------------------------------------------------------------
    def _make_agent(self, agent_name: str):
        """Hook point: tests replace this to observe/capture agent traffic."""
        return self.runtime.agent(agent_name)

    def _validate_stages(self, stages: list[str]) -> None:
        if not stages:
            raise ConfigError("Pipeline has no stages to run")
        for name in stages:
            if name not in STAGE_SPECS:
                known = ", ".join(STAGE_SPECS)
                raise ConfigError(
                    f"Unknown pipeline stage '{name}'. Known stages: {known}"
                )
            # raises ConfigError when the stage's agent is missing from config
            self.runtime.registry.agent(STAGE_SPECS[name].agent)

    def _resolve_size(self, goal: str) -> str:
        """Pick the stage list for this run. Explicit ``stages=`` skips triage."""
        if self._stage_override is not None:
            return "custom"
        size = triage.classify(goal) if self.triage_enabled else "medium"
        self.stages = list(triage.FAST_LANE_STAGES) if size == "small" else list(self._default_stages)
        self._validate_stages(self.stages)
        return size

    def _message_for(self, stage: str, goal: str, *, fix: bool) -> str:
        spec = STAGE_SPECS[stage]
        if fix and spec.fix_instruction:
            return spec.fix_instruction
        return spec.instruction or goal

    def _context_for(self, board: TaskBoard, stage: str) -> dict[str, str]:
        spec = STAGE_SPECS[stage]
        context: dict[str, str] = {}
        for need in spec.needs:
            text = board.rendered_files(need) if need in FILE_STAGES else None
            if text is None:
                text = board.artifact(need)
            if text is not None:
                context[need] = text
        if spec.wants_execution:
            block = board.execution_context()
            if block:
                context["execution"] = block
        return context

    def _alternate_model(self, agent_name: str, board: TaskBoard) -> list[Candidate] | None:
        """The rest of the routing chain after whichever candidate just answered.

        Used for loop detection (D-loop-fix): the same error twice in a row
        means retrying the same model is unlikely to help, so the next fix
        round is forced onto a different provider/model.
        """
        chain = self.runtime.registry.candidate_chain(agent_name)
        if len(chain) < 2:
            return None
        last_target = board.get(agent_name).target
        for index, (spec, model) in enumerate(chain):
            if f"{spec.name}/{model.id}" == last_target:
                return chain[index + 1 :] or None
        return chain[1:] or None

    # -- workspace (Phase 3) -------------------------------------------------
    def _apply(self, board: TaskBoard) -> ApplyReport:
        """Write the board's file manifests into ``workspace/<project>/``."""
        if not self.apply_workspace:
            return ApplyReport(
                project=self._project, root=str(self.workspace.root), dry_run=True
            )
        report = apply_board(board, self.workspace, self._project, dry_run=self.dry_run)
        self._last_apply = report
        self.runtime.bus.emit(
            "workspace.apply",
            report.summary(),
            project=self._project,
            path=report.root,
            **report.counts(),
        )
        if report.rejected:
            self.runtime.bus.emit(
                "workspace.rejected",
                "; ".join(f"{o.path}: {o.note}" for o in report.rejected),
                project=self._project,
                rejected=len(report.rejected),
            )
        return report

    # -- approval gates (Phase 4, D21) ---------------------------------------
    def _approval(self, gate: str, payload: dict[str, Any]) -> None:
        """Block at an approval gate until a human decides (or cancels).

        A no-op unless this run asked for the gate *and* someone injected a
        hook — the CLI passes neither, so it never pauses. With a hook, this
        call blocks the worker thread on the hook's ``threading.Event``: approve
        and reject resolve it, and cancel sets the same event, so a cancel wakes
        the gate immediately rather than at the next poll. The hook raises
        :class:`ApprovalRejected` or :class:`RunCancelled` when it does not
        return normally.
        """
        if gate not in self.approval_gates or self.approval_hook is None:
            return
        if self._cancelled():
            raise RunCancelled(self.runtime.run_id, gate)
        self.runtime.bus.emit(
            "approval.waiting",
            f"waiting for approval: {gate}",
            gate=gate,
            project=self._project,
            **payload,
        )
        self.approval_hook(gate, payload)
        self.runtime.bus.emit("approval.approved", f"{gate} approved", gate=gate)

    # -- cancellation (Phase 4) ----------------------------------------------
    def _cancelled(self) -> bool:
        """Has a cancel been requested? ``False`` when nobody is watching (CLI)."""
        return self.cancel_check is not None and self.cancel_check()

    @staticmethod
    def _last_started(board: TaskBoard) -> str | None:
        """The most recent stage that is no longer pending, for ``skip_rest``."""
        for stage in reversed(board.order):
            if board.get(stage).status != PENDING:
                return stage
        return None

    # -- execution ----------------------------------------------------------
    def run(self, goal: str) -> PipelineResult:
        self._project = self._project or slugify(goal)
        self.task_size = self._resolve_size(goal)
        board = TaskBoard.new(
            run_id=self.runtime.run_id,
            goal=goal,
            stages=self.stages,
            agents={name: STAGE_SPECS[name].agent for name in self.stages},
            project=self._project,
            status=BOARD_RUNNING,
        )
        board.save(self.board_path)
        bus = self.runtime.bus
        bus.emit(
            "pipeline.start",
            goal[:160],
            stages=" -> ".join(self.stages),
            project=self._project,
            size=self.task_size,
        )
        try:
            if self.task_size == "small":
                return self._run_fast_lane(board, goal)
            return self._run_full_pipeline(board, goal)
        except RunCancelled as exc:
            # Cancelled mid-run or while blocked at an approval gate: nothing
            # failed, so no stage is marked failed - the rest is skipped and
            # the board says cancelled.
            board.skip_rest(after=self._last_started(board))
            bus.emit("pipeline.cancelled", str(exc), stage=exc.stage)
            return self._finish(board, ok=False, reason="cancelled", verdict=self._verdict(board))
        except ApprovalRejected as exc:
            # A human said no at a gate: not a stage failure either, but not a
            # cancellation - the run ends "failed" with reason "rejected" and
            # the note travels with it (the API turns it into the error text).
            board.skip_rest(after=self._last_started(board))
            bus.emit("approval.rejected", str(exc), gate=exc.gate, note=exc.note)
            return self._finish(board, ok=False, reason="rejected", verdict=self._verdict(board))
        except PipelineEscalated as exc:
            # The auto-check or review loop ran out of tries. Not a crash: stop
            # cleanly with the current code and the exact error attached (D4).
            board.skip_rest(after=self._last_started(board))
            bus.emit(
                "pipeline.escalated",
                f"escalating to a human after repeated '{exc.reason}' failures at '{exc.stage}'",
                stage=exc.stage,
                check=exc.reason,
            )
            return self._finish(
                board,
                ok=False,
                reason="escalated",
                verdict=self._verdict(board),
                escalation={
                    "stage": exc.stage,
                    "reason": exc.reason,
                    "error": exc.error,
                    "code": exc.code,
                },
            )
        except AgentSystemError as exc:
            failed = board.failed_stage
            if failed is None:  # error outside a stage execution; be defensive
                failed = self.stages[0]
                board.fail(failed, error=str(exc))
            board.skip_rest(after=failed)
            # RunCancelled, ApprovalRejected and PipelineEscalated are handled
            # above; anything here really did stop the run at a failing stage.
            board.status = BOARD_FAILED
            board.save()
            bus.emit("error", str(exc), stage=failed)
            bus.emit(
                "pipeline.end",
                f"stopped at {failed}",
                ok=False,
                reason="failed",
                board=str(board.path or self.board_path),
            )
            raise

    def _run_fast_lane(self, board: TaskBoard, goal: str) -> PipelineResult:
        """Small task: Coder -> auto-checks -> done. No review, no ceremony."""
        if self._cancelled():
            self.runtime.bus.emit("pipeline.cancelled", "cancelled before coder", stage="coder")
            board.skip_rest(after=None)
            return self._finish(board, ok=False, reason="cancelled", verdict=None)
        # No planner/architect to translate the goal into ### plan/### design,
        # so the fast lane hands it to the coder directly as context - without
        # this, the coder sees only the generic "implement everything"
        # instruction and has no idea what "everything" means.
        self._execute(board, "coder", goal=goal, context={"request": goal})
        self._apply(board)
        if self.run_tests:
            self._autocheck_gate(board, goal)
        return self._finish(board, ok=True, reason="ok", verdict=None)

    def _run_full_pipeline(self, board: TaskBoard, goal: str) -> PipelineResult:
        bus = self.runtime.bus
        verdict: str | None = None
        for stage in self.stages:
            if self._cancelled():
                # Cooperative stop: the pipeline cannot interrupt a provider
                # HTTP call in flight, so cancel takes effect at the next
                # stage boundary (or immediately, mid-command, via the
                # runner's tree kill).
                bus.emit("pipeline.cancelled", f"cancelled before {stage}", stage=stage)
                board.skip_rest(after=self._last_started(board))
                return self._finish(board, ok=False, reason="cancelled", verdict=verdict)
            context = self._context_for(board, stage)
            self._execute(board, stage, goal=goal, context=context)
            # Phase 3: real files, then real evidence
            if stage in FILE_STAGES:
                self._apply(board)
            # Phase 4: pause for a human at this stage's gate (D21)
            gate = _STAGE_GATES.get(stage)
            if gate is not None:
                self._approval(
                    gate,
                    {"stage": stage, "output": board.get(stage).text[:4000]},
                )
            if stage == "tester" and self.run_tests:
                # Deterministic gate right after the tests are written: the
                # reviewer (if any) never sees code that does not even compile.
                self._autocheck_gate(board, goal)
            if stage == "reviewer":
                verdict = self._review_loop(board, goal)
        return self._finish(board, ok=True, reason="ok", verdict=verdict)

    def _execute(
        self,
        board: TaskBoard,
        stage: str,
        *,
        goal: str,
        context: dict[str, str],
        fix: bool = False,
        override: list[Candidate] | None = None,
    ) -> None:
        spec = STAGE_SPECS[stage]
        board.start(stage, agent=spec.agent)
        self.runtime.bus.emit(
            "stage.start", f"running {stage}", stage=stage, agent=spec.agent
        )
        try:
            agent = self._make_agent(spec.agent)
            result = agent.run(
                self._message_for(stage, goal, fix=fix),
                context=context or None,
                override=override or self.agent_overrides.get(spec.agent) or self.override,
            )
        except AgentSystemError as exc:
            board.fail(stage, error=str(exc))
            self.runtime.bus.emit(
                "stage.failed", str(exc), stage=stage, agent=spec.agent
            )
            raise

        note: str | None = None
        if stage == "reviewer" and result.parsed is None:
            note = (
                "reviewer output had no parseable JSON block; "
                "treated as approval so the pipeline can finish"
            )
        completion = result.completion
        record = board.complete(
            stage,
            text=result.text,
            parsed=result.parsed,
            target=completion.target,
            tokens=completion.usage.total_tokens,
            latency_ms=completion.latency_ms,
            note=note,
        )
        self.runtime.bus.emit(
            "stage.end",
            f"finished {stage} (attempt {record.attempts})",
            stage=stage,
            agent=spec.agent,
            provider=completion.provider,
            model=completion.model,
            tokens=completion.usage.total_tokens,
            parsed_json=result.parsed is not None,
        )

    # -- minimal-patch coder fix (D5) -----------------------------------------
    def _coder_fix(
        self,
        board: TaskBoard,
        goal: str,
        *,
        error: str,
        history: str,
        override: list[Candidate] | None = None,
    ) -> None:
        """Re-run the coder with ONLY the current code, the exact error and a
        one-line history - never the full plan/design/review context. Keeps
        fix rounds cheap and pushes the coder toward a minimal patch instead
        of a full rewrite."""
        context = {
            "code": board.rendered_files("coder") or board.artifact("coder") or "",
            "error": error[:6000],
            "history": history,
        }
        self._execute(board, "coder", goal=goal, context=context, fix=True, override=override)

    def _escalate(self, board: TaskBoard, *, stage: str, reason: str, error: str) -> None:
        code = board.rendered_files("coder") or board.artifact("coder") or ""
        raise PipelineEscalated(stage=stage, reason=reason, error=error, code=code)

    # -- auto-check gate (D2): py_compile -> ruff -> pytest ------------------
    def _autocheck_gate(self, board: TaskBoard, goal: str) -> None:
        """Deterministic checks before the reviewer (or the fast lane) trusts
        the code. A failure goes back to the coder as an exact-error, minimal
        patch; the same error twice in a row switches models; after
        ``max_autocheck_iterations`` tries this escalates instead of dying as
        a bare "failed"."""
        if not self.execution.enabled:
            self.runtime.bus.emit(
                "exec.skipped", "command execution is disabled (execution.enabled: false)"
            )
            return
        project_dir = self.workspace.project_dir(self._project)
        seen: list[str] = []
        tries = 0
        while True:
            outcome = self._run_checks(board, project_dir)
            if outcome is None:
                return
            check, result = outcome
            if result.cancelled:
                raise RunCancelled(self.runtime.run_id, "coder")
            error_text = result.context_block()
            if tries >= self.max_autocheck_iterations:
                self._escalate(board, stage="coder", reason=f"{check}_failed", error=error_text)
            tries += 1
            signature = _signature(error_text)
            repeated = signature in seen
            seen.append(signature)
            self.runtime.bus.emit(
                "pipeline.autocheck_failed",
                f"{check} failed; fix attempt {tries}/{self.max_autocheck_iterations}"
                + (" (same error again - switching model)" if repeated else ""),
                check=check,
                attempt=tries,
                max_attempts=self.max_autocheck_iterations,
            )
            override = self._alternate_model("coder", board) if repeated else None
            history = f"auto-check attempt {tries}/{self.max_autocheck_iterations}: {check} failed"
            try:
                self._coder_fix(board, goal, error=error_text, history=history, override=override)
            except AllProvidersFailed as exc:
                # A gateway failure (every provider rate-limited/down) is not a
                # code problem - it does not consume an auto-check attempt, it
                # ends the loop right away since nothing can fix the code.
                self._escalate(board, stage="coder", reason="gateway_exhausted", error=str(exc))
            self._apply(board)

    def _run_checks(
        self, board: TaskBoard, project_dir: Path
    ) -> tuple[str, CommandResult] | None:
        """Run compile -> lint -> test, in order, stopping at the first failure."""
        commands = list(_PRE_TEST_CHECKS)
        test_command, _source = test_command_for(board, project_dir)
        if test_command:
            commands = [*commands, ("test", test_command)]
        else:
            self.runtime.bus.emit(
                "exec.skipped",
                "no test command: the tester did not name one and none could be detected",
            )
        # Phase 4: one gate for the whole batch (not one per check) - the exact
        # test command, its working directory and the timeout, before anything
        # starts (D21).
        self._approval(
            "execution",
            {
                "command": test_command or (commands[0][1] if commands else ""),
                "commands": [command for _, command in commands],
                "cwd": str(project_dir),
                "timeout_seconds": self.execution.timeout_seconds,
            },
        )
        for check, command in commands:
            self.runtime.bus.emit(
                "exec.start",
                f"{command}  ({check})",
                project=self._project,
                command=command,
                check=check,
            )
            try:
                result = self.runner.run(command, cwd=project_dir)
            except CommandNotAllowed as exc:
                # a command we refuse to run is not a crash: skip it, loudly
                self.runtime.bus.emit("exec.skipped", str(exc), command=command)
                continue
            board.add_execution(result)
            self.runtime.bus.emit(
                "exec.end",
                f"{command}: {result.summary()}",
                project=self._project,
                command=command,
                check=check,
                ok=result.ok,
                exit_code=result.exit_code,
                duration_ms=result.duration_ms,
                timed_out=result.timed_out,
                truncated=result.stdout_truncated or result.stderr_truncated,
            )
            if not result.ok:
                return check, result
        return None

    # -- review loop (D3): only a blocker can reject -------------------------
    def _review_loop(self, board: TaskBoard, goal: str) -> str | None:
        """Coder<->reviewer rounds while a reviewer BLOCKER stands, up to
        ``max_review_iterations``. Exhausting it escalates rather than
        returning a bare "changes requested" for the caller to puzzle over."""
        verdict = self._verdict(board)
        seen: list[str] = []
        rounds = 0
        while (
            verdict == "changes_requested"
            and "coder" in self.stages
            and rounds < self.max_review_iterations
        ):
            rounds += 1
            blockers = self._blockers_text(board)
            signature = _signature(blockers)
            repeated = signature in seen
            seen.append(signature)
            self.runtime.bus.emit(
                "pipeline.fix",
                f"reviewer blocked the change; fix round {rounds}/{self.max_review_iterations}"
                + (" (same blockers again - switching model)" if repeated else ""),
            )
            override = self._alternate_model("coder", board) if repeated else None
            history = f"review round {rounds}/{self.max_review_iterations}: blockers unresolved"
            try:
                self._coder_fix(board, goal, error=blockers, history=history, override=override)
            except AllProvidersFailed as exc:
                self._escalate(board, stage="coder", reason="gateway_exhausted", error=str(exc))
            self._apply(board)
            if self.run_tests:
                self._autocheck_gate(board, goal)
            self._execute(board, "reviewer", goal=goal, context=self._context_for(board, "reviewer"))
            verdict = self._verdict(board)
        if verdict == "changes_requested":
            self._escalate(board, stage="reviewer", reason="review_blockers", error=self._blockers_text(board))
        return verdict

    @staticmethod
    def _blockers_text(board: TaskBoard) -> str:
        record = board.get("reviewer")
        parsed = record.parsed if isinstance(record.parsed, dict) else {}
        blockers = parsed.get("blockers")
        if isinstance(blockers, list) and blockers:
            return "\n".join(f"- {b}" for b in blockers)
        return record.text

    @staticmethod
    def _verdict(board: TaskBoard) -> str | None:
        """Normalise the reviewer's board verdict into approve/changes/None.

        ``None`` means "no usable verdict" (unparsed or unknown wording) and is
        deliberately treated as pass: a formatting quirk of a free model must
        never deadlock the pipeline - the note is on the board for a human.
        Only a verdict with at least one blocker may reject (D3): a "reject"
        with an empty blockers list is a prompt-following slip, not a real one.
        """
        record = board.get("reviewer")
        raw = board.verdict()
        if raw in _REJECT:
            blockers = record.parsed.get("blockers") if isinstance(record.parsed, dict) else None
            if not blockers:
                record.notes.append(
                    "verdict was 'reject' with no blockers listed; treated as approval"
                )
                return "approve"
            return "changes_requested"
        if raw in _APPROVE:
            return "approve"
        if raw is not None:
            record.notes.append(f"unrecognised verdict '{raw}'; treated as approval")
        return None

    # -- finish -------------------------------------------------------------
    def _finish(
        self,
        board: TaskBoard,
        *,
        ok: bool,
        reason: str,
        verdict: str | None,
        escalation: dict[str, Any] | None = None,
    ) -> PipelineResult:
        # The board's own status, not the stage records': the API's GET reports it
        # verbatim, so a cancelled run stays visibly cancelled.
        board.status = BOARD_CANCELLED if reason == "cancelled" else (
            BOARD_SUCCEEDED if ok else BOARD_FAILED
        )
        saved = board.save()
        budget = self.runtime.budget.state
        result = PipelineResult(
            run_id=self.runtime.run_id,
            goal=board.goal,
            ok=ok,
            reason=reason,
            stages=list(self.stages),
            verdict=verdict,
            board_path=str(saved),
            events_file=str(self.runtime.events_path),
            budget={"calls": budget.calls, "tokens": budget.tokens},
            project=board.project,
            workspace=str(self.workspace.project_dir(self._project, create=False)),
            files=(self._last_apply.counts() if self._last_apply else {}),
            tests=board.last_execution,
            size=self.task_size,
            escalation=escalation,
        )
        self.runtime.bus.emit(
            "pipeline.end",
            "completed" if ok else f"stopped ({reason})",
            ok=ok,
            reason=reason,
            verdict=verdict,
            board=str(saved),
        )
        return result

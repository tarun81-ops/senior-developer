"""The orchestrator: runs specialist stages in order over a shared task board.

Flow (stage order comes from ``pipeline:`` in config/agents.yaml)::

    planner -> architect -> coder -> tester -> reviewer
        --(changes_requested: coder fixes, reviewer re-checks,
            at most max_fix_iterations times)-->
        --(still changes_requested: stop for a human)-->
    devops -> docs

Design rules this module keeps:

  * The agent class does not know it is part of a pipeline; the pipeline tells
    each agent what to do and stores the result on the board.
  * Stage order, the fix-loop limit and every model choice live in YAML, not
    here — changing the process is a config edit (D6).
  * Every stage transition goes through the event bus, so the CLI shows the
    run live and Phase 4's UI gets the same events over a WebSocket.
  * Any :class:`AgentSystemError` (budget, provider, config) stops the run:
    the failing stage is marked, the rest are skipped, the board is saved, and
    the error is re-raised for the CLI to map to an exit code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from backend.core.errors import AgentSystemError, ConfigError
from backend.core.orchestrator.board import TaskBoard
from backend.core.workspace import (
    FILE_STAGES,
    ApplyReport,
    CommandNotAllowed,
    CommandRunner,
    Workspace,
    apply_board,
    slugify,
    test_command_for,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a runtime cycle
    from backend.core.runtime import Runtime


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
    #: set for stages that can be re-run when the reviewer objects (coder)
    fix_instruction: str | None = None
    fix_needs: tuple[str, ...] | None = None
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
            "The reviewer requested changes (see ### review in CONTEXT) and/or the "
            "test run failed (see ### execution in CONTEXT). Apply every requested "
            "fix and make the failing tests pass, then re-emit the complete JSON "
            "file manifest with ALL files (not only the changed ones)."
        ),
        fix_needs=("planner", "architect", "coder", "reviewer"),
        wants_execution=True,
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
#: onto pipeline behaviour
_APPROVE = {"approve", "approved", "lgtm"}
_REJECT = {"changes_requested", "changes requested", "revise", "reject", "rejected"}


@dataclass
class PipelineResult:
    """Everything the CLI/UI needs after a pipeline run."""

    run_id: str
    goal: str
    ok: bool
    reason: str  # "ok" | "review" | "tests"
    stages: list[str]
    verdict: str | None
    board_path: str
    events_file: str = ""
    budget: dict[str, int] = field(default_factory=dict)
    #: Phase 3 — where the code went and what running it said
    project: str = ""
    workspace: str = ""
    files: dict[str, int] = field(default_factory=dict)
    tests: dict[str, Any] | None = None

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
        }


class Pipeline:
    """Executes a stage sequence against a runtime, recording everything."""

    def __init__(
        self,
        runtime: Runtime,
        *,
        stages: list[str] | None = None,
        max_fix_iterations: int | None = None,
        board_path: Path | str | None = None,
        override: list[tuple[Any, Any]] | None = None,
        project: str | None = None,
        apply_workspace: bool | None = None,
        run_tests: bool | None = None,
        dry_run: bool = False,
    ) -> None:
        self.runtime = runtime
        config = runtime.registry.pipeline
        self.stages = list(stages or config.stages)
        self.max_fix_iterations = (
            config.max_fix_iterations if max_fix_iterations is None else max_fix_iterations
        )
        if not self.stages:
            raise ConfigError("Pipeline has no stages to run")
        if self.max_fix_iterations < 0:
            raise ConfigError("pipeline.max_fix_iterations must be >= 0")
        for name in self.stages:
            if name not in STAGE_SPECS:
                known = ", ".join(STAGE_SPECS)
                raise ConfigError(
                    f"Unknown pipeline stage '{name}'. Known stages: {known}"
                )
            # raises ConfigError when the stage's agent is missing from config
            runtime.registry.agent(STAGE_SPECS[name].agent)
        self.override = override
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
        self.runner = CommandRunner(self.execution)
        self._project = slugify(project) if project else ""
        self._last_apply: ApplyReport | None = None

    # -- pieces -------------------------------------------------------------
    def _make_agent(self, agent_name: str):
        """Hook point: tests replace this to observe/capture agent traffic."""
        return self.runtime.agent(agent_name)

    def _message_for(self, stage: str, goal: str, *, fix: bool) -> str:
        spec = STAGE_SPECS[stage]
        if fix and spec.fix_instruction:
            return spec.fix_instruction
        return spec.instruction or goal

    def _context_for(self, board: TaskBoard, stage: str, *, fix: bool) -> dict[str, str]:
        spec = STAGE_SPECS[stage]
        needs = spec.fix_needs if (fix and spec.fix_needs) else spec.needs
        # read BEFORE the stage overwrites its own record, so a coder fix round
        # still sees its previous artifact
        context = board.artifacts_for(needs)
        if spec.wants_execution:
            block = board.execution_context()
            if block:
                context["execution"] = block
        return context

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

    def _run_tests(self, board: TaskBoard) -> None:
        """Run the project's test command and put the result on the board."""
        if not self.execution.enabled:
            self.runtime.bus.emit(
                "exec.skipped", "command execution is disabled (execution.enabled: false)"
            )
            return
        project_dir = self.workspace.project_dir(self._project)
        command, source = test_command_for(board, project_dir)
        if not command:
            self.runtime.bus.emit(
                "exec.skipped",
                "no test command: the tester did not name one and none could be detected",
            )
            return
        self.runtime.bus.emit(
            "exec.start",
            f"{command}  (from {source})",
            project=self._project,
            command=command,
            source=source,
        )
        try:
            result = self.runner.run(command, cwd=project_dir)
        except CommandNotAllowed as exc:
            # a command we refuse to run is not a crash: skip it, loudly
            self.runtime.bus.emit("exec.skipped", str(exc), command=command)
            return
        board.add_execution(result)
        self.runtime.bus.emit(
            "exec.end",
            f"{command}: {result.summary()}",
            project=self._project,
            command=command,
            ok=result.ok,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            timed_out=result.timed_out,
            truncated=result.stdout_truncated or result.stderr_truncated,
        )

    # -- execution ----------------------------------------------------------
    def run(self, goal: str) -> PipelineResult:
        self._project = self._project or slugify(goal)
        board = TaskBoard.new(
            run_id=self.runtime.run_id,
            goal=goal,
            stages=self.stages,
            agents={name: STAGE_SPECS[name].agent for name in self.stages},
            project=self._project,
        )
        board.save(self.board_path)
        bus = self.runtime.bus
        bus.emit(
            "pipeline.start",
            goal[:160],
            stages=" -> ".join(self.stages),
            project=self._project,
        )
        try:
            verdict: str | None = None
            for index, stage in enumerate(self.stages):
                remaining = self.stages[index + 1 :]
                context = self._context_for(board, stage, fix=False)
                self._execute(board, stage, goal=goal, context=context)
                # Phase 3: real files, then real evidence
                if stage in FILE_STAGES:
                    self._apply(board)
                if stage == "tester" and self.run_tests:
                    self._run_tests(board)
                if stage == "reviewer":
                    verdict = self._review_loop(board, goal)
                stop = self._stop_reason(board, verdict, remaining)
                if stop:
                    if stop == "tests":
                        last = board.last_execution or {}
                        self.runtime.bus.emit(
                            "pipeline.unresolved_tests",
                            f"test run still failing ({last.get('command')}); "
                            "stopping for a human",
                        )
                    board.skip_rest(after=stage)
                    return self._finish(board, ok=False, reason=stop, verdict=verdict)
            return self._finish(board, ok=True, reason="ok", verdict=verdict)
        except AgentSystemError as exc:
            failed = board.failed_stage
            if failed is None:  # error outside a stage execution; be defensive
                failed = self.stages[0]
                board.fail(failed, error=str(exc))
            board.skip_rest(after=failed)
            bus.emit("error", str(exc), stage=failed)
            bus.emit(
                "pipeline.end",
                f"stopped at {failed}",
                ok=False,
                reason="failed",
                board=str(board.path or self.board_path),
            )
            raise

    def _execute(
        self,
        board: TaskBoard,
        stage: str,
        *,
        goal: str,
        context: dict[str, str],
        fix: bool = False,
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
                override=self.override,
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

    def _review_loop(self, board: TaskBoard, goal: str) -> str | None:
        """Run coder fix iterations while the reviewer objects or tests fail."""
        verdict = self._verdict(board)
        iterations = 0
        while iterations < self.max_fix_iterations and "coder" in self.stages:
            reason = self._fix_reason(board, verdict)
            if reason is None:
                break
            iterations += 1
            self.runtime.bus.emit(
                "pipeline.fix",
                f"{reason}; fix iteration {iterations}/{self.max_fix_iterations}",
            )
            self._execute(
                board,
                "coder",
                goal=goal,
                context=self._context_for(board, "coder", fix=True),
                fix=True,
            )
            # the new code has to be on disk and re-tested before the reviewer
            # is asked again: evidence, not claims
            self._apply(board)
            if self.run_tests:
                self._run_tests(board)
            self._execute(
                board,
                "reviewer",
                goal=goal,
                context=self._context_for(board, "reviewer", fix=False),
            )
            verdict = self._verdict(board)
        self._emit_unresolved_review(board, verdict, iterations)
        return verdict

    def _fix_reason(self, board: TaskBoard, verdict: str | None) -> str | None:
        """Why another coder round is warranted, or ``None`` if it is not."""
        if verdict == "changes_requested":
            return "review requested changes"
        if self.run_tests and board.tests_failed:
            return "the test run failed"
        return None

    def _stop_reason(
        self, board: TaskBoard, verdict: str | None, remaining: list[str]
    ) -> str | None:
        """Should the pipeline stop before the remaining stages? (D8, D14, D15)"""
        if verdict == "changes_requested":
            return "review"
        if self.run_tests and board.tests_failed:
            # a fix round may still be possible: let the loop try first
            if "reviewer" in remaining or "coder" in remaining:
                return None
            return "tests"
        return None

    def _emit_unresolved_review(
        self, board: TaskBoard, verdict: str | None, iterations: int
    ) -> None:
        if verdict != "changes_requested":
            return
        if "coder" not in self.stages:
            detail = "but the coder stage is not part of this run"
        elif self.max_fix_iterations == 0:
            detail = "and the fix loop is disabled (max_fix_iterations: 0)"
        else:
            detail = f"after {iterations} fix iteration(s)"
        self.runtime.bus.emit(
            "pipeline.unresolved_review",
            f"reviewer requests changes {detail}; stopping for a human",
        )

    @staticmethod
    def _verdict(board: TaskBoard) -> str | None:
        """Normalise the reviewer's board verdict into approve/changes/None.

        ``None`` means "no usable verdict" (unparsed or unknown wording) and is
        deliberately treated as pass: a formatting quirk of a free model must
        never deadlock the pipeline — the note is on the board for a human.
        """
        raw = board.verdict()
        if raw in _REJECT:
            return "changes_requested"
        if raw in _APPROVE:
            return "approve"
        if raw is not None:
            board.get("reviewer").notes.append(
                f"unrecognised verdict '{raw}'; treated as approval"
            )
        return None

    # -- finish -------------------------------------------------------------
    def _finish(
        self, board: TaskBoard, *, ok: bool, reason: str, verdict: str | None
    ) -> PipelineResult:
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

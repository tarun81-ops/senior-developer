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

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a runtime cycle
    from backend.core.runtime import Runtime


@dataclass(frozen=True)
class StageSpec:
    """What one stage is: which agent runs it, what it needs, what to ask.

    ``instruction`` is the user message sent to the agent; when it is empty
    (planner) the goal itself is the message. ``needs`` names whose completed
    board artifacts become this stage's CONTEXT.
    """

    agent: str
    instruction: str
    needs: tuple[str, ...] = ()
    #: set for stages that can be re-run when the reviewer objects (coder)
    fix_instruction: str | None = None
    fix_needs: tuple[str, ...] | None = None


STAGE_SPECS: dict[str, StageSpec] = {
    "planner": StageSpec(agent="planner", instruction=""),
    "architect": StageSpec(
        agent="architect",
        instruction="Produce the system design for this request.",
        needs=("planner",),
    ),
    "coder": StageSpec(
        agent="coder",
        instruction="Implement every file needed for this request. Output only the JSON file manifest.",
        needs=("planner", "architect"),
        fix_instruction=(
            "The reviewer requested changes (see ### review in CONTEXT). Apply every "
            "requested fix and re-emit the complete JSON file manifest with ALL files "
            "(not only the changed ones)."
        ),
        fix_needs=("planner", "architect", "coder", "reviewer"),
    ),
    "tester": StageSpec(
        agent="tester",
        instruction="Write the test suite for the implementation in CONTEXT.",
        needs=("architect", "coder"),
    ),
    "reviewer": StageSpec(
        agent="reviewer",
        instruction=(
            "Review the implementation in CONTEXT against the design and tests. "
            "Output only the verdict JSON."
        ),
        needs=("architect", "coder", "tester"),
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
    reason: str  # "ok" | "review"
    stages: list[str]
    verdict: str | None
    board_path: str
    events_file: str = ""
    budget: dict[str, int] = field(default_factory=dict)

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
        return board.artifacts_for(needs)

    # -- execution ----------------------------------------------------------
    def run(self, goal: str) -> PipelineResult:
        board = TaskBoard.new(
            run_id=self.runtime.run_id,
            goal=goal,
            stages=self.stages,
            agents={name: STAGE_SPECS[name].agent for name in self.stages},
        )
        board.save(self.board_path)
        bus = self.runtime.bus
        bus.emit("pipeline.start", goal[:160], stages=" -> ".join(self.stages))
        try:
            verdict: str | None = None
            for stage in self.stages:
                context = self._context_for(board, stage, fix=False)
                self._execute(board, stage, goal=goal, context=context)
                if stage == "reviewer":
                    verdict = self._review_loop(board, goal)
                    if verdict == "changes_requested":
                        # out of fix iterations: a human has to decide (D8)
                        board.skip_rest(after="reviewer")
                        return self._finish(
                            board, ok=False, reason="review", verdict=verdict
                        )
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
        """Run coder fix iterations while the reviewer keeps objecting."""
        verdict = self._verdict(board)
        iterations = 0
        while (
            verdict == "changes_requested"
            and iterations < self.max_fix_iterations
            and "coder" in self.stages
        ):
            iterations += 1
            self.runtime.bus.emit(
                "pipeline.fix",
                f"review requested changes; fix iteration "
                f"{iterations}/{self.max_fix_iterations}",
            )
            self._execute(
                board,
                "coder",
                goal=goal,
                context=self._context_for(board, "coder", fix=True),
                fix=True,
            )
            self._execute(
                board,
                "reviewer",
                goal=goal,
                context=self._context_for(board, "reviewer", fix=False),
            )
            verdict = self._verdict(board)
        if verdict == "changes_requested":
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
        return verdict

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

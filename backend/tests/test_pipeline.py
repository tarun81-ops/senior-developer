"""Phase 2 orchestrator tests: task board, stage order, fix loop, budgets.

Everything runs offline against mock providers — the pipeline mechanics (order,
context wiring, review gating, persistence, budget stops) are what we guard
here; token spending is tested once and cheaply, never with live keys.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.core.agents import AgentResult, extract_json_block
from backend.core.errors import AllProvidersFailed, BudgetExceeded, ConfigError
from backend.core.orchestrator import Pipeline, TaskBoard
from backend.core.orchestrator.board import DONE, FAILED, PENDING, SKIPPED
from backend.core.provider.schemas import Completion, Usage
from backend.tests.helpers import build_pipeline_runtime


class RecordingPipeline(Pipeline):
    """A pipeline that records every stage call before executing it."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[dict] = []

    def _execute(self, board, stage, *, goal, context, fix=False):  # type: ignore[override]
        self.calls.append(
            {
                "stage": stage,
                "message": self._message_for(stage, goal, fix=fix),
                "context": dict(context),
                "fix": fix,
            }
        )
        super()._execute(board, stage, goal=goal, context=context, fix=fix)


GOAL = "build a todo app"


def test_board_round_trip_preserves_state(tmp_path: Path) -> None:
    board = TaskBoard.new(
        run_id="r1", goal="g", stages=["a", "b"], agents={"a": "agent_a"}
    )
    board.save(tmp_path)  # a board must know where it lives before it mutates
    board.start("a", agent="agent_a")
    board.complete(
        "a",
        text="hello",
        parsed={"x": 1},
        target="mock/m",
        tokens=5,
        latency_ms=2,
    )
    path = board.save(tmp_path)

    loaded = TaskBoard.load(path)
    record = loaded.records["a"]
    assert record.status == DONE
    assert record.text == "hello"
    assert record.parsed == {"x": 1}
    assert record.target == "mock/m"
    assert record.tokens == 5
    assert record.attempts == 1
    assert loaded.records["b"].status == PENDING
    assert loaded.artifact("a") == "hello"
    assert loaded.artifact("b") is None
    assert loaded.verdict() is None
    assert loaded.goal == "g"
    assert loaded.order == ["a", "b"]


def test_pipeline_runs_stages_in_order_and_wires_context(tmp_path: Path) -> None:
    runtime = build_pipeline_runtime(tmp_path)
    pipeline = RecordingPipeline(runtime, board_path=tmp_path / "board.json")

    result = pipeline.run(GOAL)

    assert result.ok is True
    assert result.reason == "ok"
    assert result.verdict == "approve"
    assert [call["stage"] for call in pipeline.calls] == [
        "planner",
        "architect",
        "coder",
        "tester",
        "reviewer",
        "devops",
        "docs",
    ]

    calls = {call["stage"]: call for call in pipeline.calls}
    # planner gets the raw goal and no context (it goes first)
    assert calls["planner"]["message"] == GOAL
    assert calls["planner"]["context"] == {}
    # each later stage sees exactly its declared predecessors' artifacts
    assert set(calls["architect"]["context"]) == {"planner"}
    assert set(calls["coder"]["context"]) == {"planner", "architect"}
    assert set(calls["tester"]["context"]) == {"architect", "coder"}
    assert set(calls["reviewer"]["context"]) == {"architect", "coder", "tester"}
    assert set(calls["devops"]["context"]) == {"planner", "coder"}
    assert set(calls["docs"]["context"]) == {"planner", "architect", "coder", "reviewer"}

    # the board on disk tells the same story
    board = TaskBoard.load(result.board_path)
    assert board.ok is True
    assert board.verdict() == "approve"
    for name in board.order:
        assert board.records[name].status == DONE
        assert board.records[name].target.startswith("mock_")


def test_changes_requested_runs_fix_loop_then_stops_for_a_human(tmp_path: Path) -> None:
    runtime = build_pipeline_runtime(
        tmp_path,
        reviewer_reply='{"verdict": "changes_requested", "issues": []}',
        max_fix_iterations=1,
    )
    pipeline = RecordingPipeline(runtime, board_path=tmp_path / "board.json")

    result = pipeline.run(GOAL)

    assert result.ok is False
    assert result.reason == "review"
    assert result.verdict == "changes_requested"

    coder_calls = [c for c in pipeline.calls if c["stage"] == "coder"]
    reviewer_calls = [c for c in pipeline.calls if c["stage"] == "reviewer"]
    assert len(coder_calls) == 2
    assert len(reviewer_calls) == 2
    assert coder_calls[0]["fix"] is False
    assert coder_calls[1]["fix"] is True
    # the fix round sees the previous code AND the review that demanded changes
    assert set(coder_calls[1]["context"]) == {"planner", "architect", "coder", "reviewer"}

    board = TaskBoard.load(result.board_path)
    assert board.records["coder"].attempts == 2
    assert board.records["reviewer"].attempts == 2
    # stages after the unresolved review never ran
    assert board.records["devops"].status == SKIPPED
    assert board.records["docs"].status == SKIPPED
    assert board.ok is False

    lines = (tmp_path / "events.jsonl").read_text().splitlines()
    kinds = [json.loads(line)["kind"] for line in lines]
    assert "pipeline.fix" in kinds
    assert "pipeline.unresolved_review" in kinds


def test_unparseable_reviewer_output_does_not_deadlock_the_pipeline(tmp_path: Path) -> None:
    runtime = build_pipeline_runtime(tmp_path, reviewer_reply="looks good to me!")
    pipeline = RecordingPipeline(runtime, board_path=tmp_path / "board.json")

    result = pipeline.run(GOAL)

    assert result.ok is True
    assert result.verdict is None
    board = TaskBoard.load(result.board_path)
    reviewer = board.records["reviewer"]
    assert reviewer.status == DONE
    assert any("no parseable JSON" in note for note in reviewer.notes)
    # no fix loop happened for an unparseable review
    assert len([c for c in pipeline.calls if c["stage"] == "coder"]) == 1


def test_budget_stop_marks_the_stage_and_skips_the_rest(tmp_path: Path) -> None:
    runtime = build_pipeline_runtime(
        tmp_path, limits={"budget": {"max_calls_per_run": 2}}
    )
    pipeline = RecordingPipeline(runtime, board_path=tmp_path / "board.json")

    with pytest.raises(BudgetExceeded):
        pipeline.run(GOAL)

    board = TaskBoard.load(tmp_path / "board.json")
    # two stages fit in the budget, the third one hits the ceiling
    assert board.records["planner"].status == DONE
    assert board.records["architect"].status == DONE
    assert board.records["coder"].status == FAILED
    assert "maximum of 2 model calls" in board.records["coder"].error
    for name in ("tester", "reviewer", "devops", "docs"):
        assert board.records[name].status == SKIPPED


def test_unknown_stage_is_a_config_error(tmp_path: Path) -> None:
    runtime = build_pipeline_runtime(tmp_path)
    with pytest.raises(ConfigError) as excinfo:
        Pipeline(runtime, stages=["planner", "nope"])
    assert "nope" in str(excinfo.value)


def test_unknown_verdict_wording_is_treated_as_approval(tmp_path: Path) -> None:
    runtime = build_pipeline_runtime(tmp_path, reviewer_reply='{"verdict": "ship it"}')
    pipeline = RecordingPipeline(runtime, board_path=tmp_path / "board.json")

    result = pipeline.run(GOAL)

    assert result.ok is True
    board = TaskBoard.load(result.board_path)
    assert any("unrecognised verdict" in note for note in board.records["reviewer"].notes)


# --------------------------------------------------------------------------- #
# Phase 3: workspace execution inside the pipeline
# --------------------------------------------------------------------------- #
class StubAgent:
    """An agent with scripted replies, so pipeline logic is testable exactly."""

    def __init__(self, name: str, replies: list[str]) -> None:
        self.name = name
        self.replies = list(replies) or ["{}"]
        self.messages: list[str] = []
        self.contexts: list[dict] = []

    def run(
        self, message, *, context=None, temperature=None, max_output_tokens=None, override=None
    ):
        self.messages.append(message)
        self.contexts.append(dict(context or {}))
        text = self.replies[min(len(self.messages) - 1, len(self.replies) - 1)]
        return AgentResult(
            agent=self.name,
            text=text,
            completion=Completion(
                text=text,
                provider="stub",
                model="stub-1",
                usage=Usage(prompt_tokens=5, completion_tokens=2, total_tokens=7),
                latency_ms=3,
            ),
            parsed=extract_json_block(text),
        )


class StubPipeline(Pipeline):
    """Replaces the agents with stubs; everything else is the real pipeline."""

    def __init__(self, *args, replies: dict[str, list[str]] | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.stub_replies = dict(replies or {})
        self.stubs: dict[str, StubAgent] = {}

    def _make_agent(self, agent_name: str) -> StubAgent:
        stub = self.stubs.get(agent_name)
        if stub is None:
            stub = StubAgent(agent_name, self.stub_replies.get(agent_name, ["{}"]))
            self.stubs[agent_name] = stub
        return stub


def _events(tmp_path: Path) -> list[str]:
    import json as _json

    path = tmp_path / "events.jsonl"
    return [_json.loads(line)["kind"] for line in path.read_text(encoding="utf-8").splitlines()]


def test_pipeline_writes_files_and_runs_the_tests(tmp_path: Path) -> None:
    runtime = build_pipeline_runtime(
        tmp_path,
        stages=["coder", "tester", "reviewer"],
        apply_workspace=True,
        run_tests=True,
    )
    pipeline = StubPipeline(
        runtime,
        board_path=tmp_path / "board.json",
        replies={
            "coder": ['{"files": [{"path": "check.py", "content": "print(\'ok\')"}]}'],
            "tester": [
                '{"run_command": "python check.py", '
                '"files": [{"path": "tests/test_x.py", "content": "def test_ok(): pass"}]}'
            ],
            "reviewer": ['{"verdict": "approve"}'],
        },
    )

    result = pipeline.run(GOAL)

    assert result.ok is True
    assert result.reason == "ok"
    project_dir = tmp_path / "project-root" / "workspace" / result.project
    assert (project_dir / "check.py").exists()
    assert (project_dir / "tests" / "test_x.py").exists()
    # counts come from the last apply: the tester's file is new, the coder's is unchanged
    assert result.files["written"] == 1
    assert result.files["unchanged"] == 1
    # the real command output is on the board and in the reviewer's context
    assert result.tests is not None and result.tests["ok"] is True
    assert "execution" in pipeline.stubs["reviewer"].contexts[0]
    assert "exit 0" in pipeline.stubs["reviewer"].contexts[0]["execution"]
    assert "exec.start" in _events(tmp_path)
    assert "workspace.apply" in _events(tmp_path)


def test_failing_tests_drive_the_fix_loop_even_when_the_review_approves(tmp_path: Path) -> None:
    runtime = build_pipeline_runtime(
        tmp_path,
        stages=["coder", "tester", "reviewer"],
        max_fix_iterations=1,
        apply_workspace=True,
        run_tests=True,
    )
    pipeline = StubPipeline(
        runtime,
        board_path=tmp_path / "board.json",
        replies={
            "coder": [
                '{"files": [{"path": "check.py", "content": "import sys; sys.exit(1)"}]}',
                '{"files": [{"path": "check.py", "content": "print(\'fixed\')"}]}',
            ],
            "tester": ['{"run_command": "python check.py"}'],
            "reviewer": ['{"verdict": "approve"}'],
        },
    )

    result = pipeline.run(GOAL)

    assert result.ok is True
    assert len(pipeline.stubs["coder"].messages) == 2  # the fix round ran
    board = TaskBoard.load(result.board_path)
    assert [entry["ok"] for entry in board.executions] == [False, True]
    assert result.tests["ok"] is True
    # the fix round was given the failing output, not a guess
    assert "execution" in pipeline.stubs["coder"].contexts[-1]
    fix_lines = [
        line
        for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if '"pipeline.fix"' in line
    ]
    assert fix_lines and "test run failed" in fix_lines[0]


def test_pipeline_stops_with_tests_reason_when_tests_keep_failing(tmp_path: Path) -> None:
    runtime = build_pipeline_runtime(
        tmp_path,
        stages=["coder", "tester", "reviewer", "devops"],
        max_fix_iterations=1,
        apply_workspace=True,
        run_tests=True,
    )
    failing = '{"files": [{"path": "check.py", "content": "import sys; sys.exit(2)"}]}'
    pipeline = StubPipeline(
        runtime,
        board_path=tmp_path / "board.json",
        replies={
            "coder": [failing, failing],
            "tester": ['{"run_command": "python check.py"}'],
            "reviewer": ['{"verdict": "approve"}'],
        },
    )

    result = pipeline.run(GOAL)

    assert result.ok is False
    assert result.reason == "tests"
    board = TaskBoard.load(result.board_path)
    assert board.records["coder"].attempts == 2
    assert all(not entry["ok"] for entry in board.executions)
    assert board.records["devops"].status == SKIPPED
    assert "pipeline.unresolved_tests" in _events(tmp_path)


def test_pipeline_skips_execution_when_there_is_no_command(tmp_path: Path) -> None:
    runtime = build_pipeline_runtime(
        tmp_path,
        stages=["coder", "tester", "reviewer"],
        apply_workspace=True,
        run_tests=True,
    )
    pipeline = StubPipeline(
        runtime,
        board_path=tmp_path / "board.json",
        replies={
            "coder": ['{"files": [{"path": "notes.md", "content": "hi"}]}'],
            "tester": ['{"summary": "I named no command"}'],
            "reviewer": ['{"verdict": "approve"}'],
        },
    )

    result = pipeline.run(GOAL)

    assert result.ok is True
    assert result.tests is None
    assert "exec.skipped" in _events(tmp_path)


def test_pipeline_dry_run_reports_files_without_writing_them(tmp_path: Path) -> None:
    runtime = build_pipeline_runtime(
        tmp_path,
        stages=["coder"],
        apply_workspace=True,
        run_tests=False,
    )
    pipeline = StubPipeline(
        runtime,
        board_path=tmp_path / "board.json",
        dry_run=True,
        replies={"coder": ['{"files": [{"path": "check.py", "content": "print(1)"}]}']},
    )

    result = pipeline.run(GOAL)

    assert result.ok is True
    assert result.files["written"] == 1  # would have been written
    assert not (tmp_path / "project-root" / "workspace" / result.project).exists()




def test_empty_reviewer_output_fails_the_stage(tmp_path: Path) -> None:
    # An empty completion is a provider failure, never a silent approval: an
    # empty review must not look like a passing review.
    runtime = build_pipeline_runtime(tmp_path, reviewer_reply="")
    pipeline = RecordingPipeline(runtime, board_path=tmp_path / "board.json")

    with pytest.raises(AllProvidersFailed):
        pipeline.run(GOAL)

    board = TaskBoard.load(tmp_path / "board.json")
    assert board.records["reviewer"].status == FAILED
    assert "empty" in board.records["reviewer"].error
    assert board.records["devops"].status == SKIPPED

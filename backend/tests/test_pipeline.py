"""Phase 2 orchestrator tests: task board, stage order, fix loop, budgets.

Everything runs offline against mock providers — the pipeline mechanics (order,
context wiring, review gating, persistence, budget stops) are what we guard
here; token spending is tested once and cheaply, never with live keys.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.core.errors import AllProvidersFailed, BudgetExceeded, ConfigError
from backend.core.orchestrator import Pipeline, TaskBoard
from backend.core.orchestrator.board import DONE, FAILED, PENDING, SKIPPED
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

    kinds = [json.loads(line)["kind"] for line in (tmp_path / "events.jsonl").read_text().splitlines()]
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

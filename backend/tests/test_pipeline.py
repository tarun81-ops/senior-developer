"""Orchestrator tests: task board, triage, auto-check loop, review loop, escalation.

Everything runs offline against mock providers — the pipeline mechanics (order,
context wiring, loop counting, escalation, persistence, budget stops) are what
we guard here; token spending is tested once and cheaply, never with live keys.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.core.agents import AgentResult, extract_json_block
from backend.core.errors import AllProvidersFailed, BudgetExceeded, ConfigError
from backend.core.orchestrator import Pipeline, TaskBoard
from backend.core.orchestrator.board import DONE, FAILED, PENDING, SKIPPED
from backend.core.orchestrator.triage import classify
from backend.core.provider.schemas import Completion, Usage
from backend.tests.helpers import build_pipeline_runtime


class RecordingPipeline(Pipeline):
    """A pipeline that records every stage call before executing it."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[dict] = []

    def _execute(self, board, stage, *, goal, context, fix=False, override=None):  # type: ignore[override]
        self.calls.append(
            {
                "stage": stage,
                "message": self._message_for(stage, goal, fix=fix),
                "context": dict(context),
                "fix": fix,
            }
        )
        super()._execute(board, stage, goal=goal, context=context, fix=fix, override=override)


GOAL = "build a todo app with a database and multiple concurrent users"


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


def test_board_accumulates_files_across_attempts(tmp_path: Path) -> None:
    """A coder fix round may emit only the files it changed; earlier files
    from a previous attempt of the same stage must not disappear."""
    board = TaskBoard.new(run_id="r1", goal="g", stages=["coder"], agents={"coder": "coder"})
    board.save(tmp_path)
    board.start("coder", agent="coder")
    board.complete(
        "coder",
        text="{}",
        parsed={"files": [{"path": "a.py", "content": "one"}, {"path": "b.py", "content": "two"}]},
        target="mock/m",
        tokens=1,
        latency_ms=1,
    )
    board.start("coder", agent="coder")
    board.complete(
        "coder",
        text="{}",
        parsed={"files": [{"path": "a.py", "content": "ONE-FIXED"}]},
        target="mock/m",
        tokens=1,
        latency_ms=1,
    )

    rendered = board.rendered_files("coder")
    assert "ONE-FIXED" in rendered
    assert "two" in rendered  # b.py survived even though the fix only touched a.py


# --------------------------------------------------------------------------- #
# triage
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("goal", "expected"),
    [
        ("a calculator", "small"),
        ("build a simple calculator app", "small"),
        ("build a todo app with a REST API and a database backend for many users", "large"),
        ("build a distributed job queue with websocket notifications", "large"),
        ("build a note-taking app that syncs across a handful of devices for one user", "medium"),
    ],
)
def test_triage_classifies_goal_size(goal: str, expected: str) -> None:
    assert classify(goal) == expected


def test_triage_routes_a_small_goal_to_the_fast_lane(tmp_path: Path) -> None:
    runtime = build_pipeline_runtime(
        tmp_path,
        triage=True,
        apply_workspace=True,
        run_tests=False,
        replies={"coder": '{"files": [{"path": "calc.py", "content": "print(1+1)"}]}'},
    )
    pipeline = RecordingPipeline(runtime, board_path=tmp_path / "board.json")

    result = pipeline.run("a calculator")

    assert result.size == "small"
    assert result.ok is True
    assert [call["stage"] for call in pipeline.calls] == ["coder"]
    # the fast lane skips planner/architect, so the coder must still see what
    # was actually asked for - not just the generic "implement everything"
    # instruction with no goal anywhere (that produced boilerplate in practice)
    assert pipeline.calls[0]["context"] == {"request": "a calculator"}
    board = TaskBoard.load(result.board_path)
    assert board.order == ["coder"]


def test_triage_routes_a_large_goal_through_the_full_pipeline(tmp_path: Path) -> None:
    runtime = build_pipeline_runtime(tmp_path, triage=True)
    pipeline = RecordingPipeline(runtime, board_path=tmp_path / "board.json")

    result = pipeline.run(GOAL)  # explicitly a "large" goal (database, multi-user)

    assert result.size == "large"
    assert [call["stage"] for call in pipeline.calls] == [
        "planner", "architect", "coder", "tester", "reviewer", "devops", "docs",
    ]


def test_explicit_stages_bypass_triage_even_for_a_small_goal(tmp_path: Path) -> None:
    runtime = build_pipeline_runtime(tmp_path, triage=True, stages=["planner"])
    pipeline = RecordingPipeline(runtime, stages=["planner"], board_path=tmp_path / "board.json")

    result = pipeline.run("a calculator")

    assert result.size == "custom"
    assert [call["stage"] for call in pipeline.calls] == ["planner"]


# --------------------------------------------------------------------------- #
# stage order and context wiring (triage disabled: helpers.py default)
# --------------------------------------------------------------------------- #
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


def test_reviewer_blocker_runs_the_fix_loop_then_escalates(tmp_path: Path) -> None:
    runtime = build_pipeline_runtime(
        tmp_path,
        reviewer_reply='{"verdict": "reject", "blockers": ["missing input validation"]}',
        max_review_iterations=1,
    )
    pipeline = RecordingPipeline(runtime, board_path=tmp_path / "board.json")

    result = pipeline.run(GOAL)

    assert result.ok is False
    assert result.reason == "escalated"
    assert result.escalation is not None
    assert result.escalation["stage"] == "reviewer"
    assert result.escalation["reason"] == "review_blockers"
    assert "missing input validation" in result.escalation["error"]
    assert result.escalation["code"]  # the current code travels with it

    coder_calls = [c for c in pipeline.calls if c["stage"] == "coder"]
    reviewer_calls = [c for c in pipeline.calls if c["stage"] == "reviewer"]
    assert len(coder_calls) == 2
    assert len(reviewer_calls) == 2
    assert coder_calls[0]["fix"] is False
    assert coder_calls[1]["fix"] is True
    # the fix round is minimal: current code + exact error + one-line history,
    # never the full plan/design context
    assert set(coder_calls[1]["context"]) == {"code", "error", "history"}
    assert "missing input validation" in coder_calls[1]["context"]["error"]

    board = TaskBoard.load(result.board_path)
    assert board.records["coder"].attempts == 2
    assert board.records["reviewer"].attempts == 2
    # stages after the escalation never ran
    assert board.records["devops"].status == SKIPPED
    assert board.records["docs"].status == SKIPPED

    lines = (tmp_path / "events.jsonl").read_text().splitlines()
    kinds = [json.loads(line)["kind"] for line in lines]
    assert "pipeline.fix" in kinds
    assert "pipeline.escalated" in kinds


def test_reviewer_reject_with_no_blockers_is_treated_as_approval(tmp_path: Path) -> None:
    runtime = build_pipeline_runtime(
        tmp_path, reviewer_reply='{"verdict": "reject", "blockers": []}'
    )
    pipeline = RecordingPipeline(runtime, board_path=tmp_path / "board.json")

    result = pipeline.run(GOAL)

    assert result.ok is True
    board = TaskBoard.load(result.board_path)
    assert any("no blockers listed" in note for note in board.records["reviewer"].notes)
    # no fix loop happened: a reject without a blocker is not a real reject
    assert len([c for c in pipeline.calls if c["stage"] == "coder"]) == 1


def test_reviewer_suggestions_never_block_approval(tmp_path: Path) -> None:
    runtime = build_pipeline_runtime(
        tmp_path,
        reviewer_reply='{"verdict": "approve", "suggestions": ["rename this variable"]}',
    )
    pipeline = RecordingPipeline(runtime, board_path=tmp_path / "board.json")

    result = pipeline.run(GOAL)

    assert result.ok is True
    assert result.verdict == "approve"


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


def test_repeated_review_blocker_switches_the_model_before_the_last_try(tmp_path: Path) -> None:
    """Loop detection (D-loop-fix): the identical blocker twice in a row means
    the next fix round is forced onto a different provider/model."""
    runtime = build_pipeline_runtime(
        tmp_path,
        reviewer_reply='{"verdict": "reject", "blockers": ["still crashes on empty input"]}',
        max_review_iterations=2,
        coder_fallback=True,
    )
    pipeline = RecordingPipeline(runtime, board_path=tmp_path / "board.json")

    result = pipeline.run(GOAL)

    assert result.reason == "escalated"
    board = TaskBoard.load(result.board_path)
    # 1 initial + 2 fix rounds
    assert board.records["coder"].attempts == 3
    lines = (tmp_path / "events.jsonl").read_text().splitlines()
    events = [json.loads(line) for line in lines]
    switched = [e for e in events if e["kind"] == "pipeline.fix" and "switching model" in e["message"]]
    assert switched, "the second identical blocker should have triggered a model switch"


def test_gateway_exhaustion_during_a_fix_round_escalates_without_extra_tries(tmp_path: Path) -> None:
    """A provider-chain failure during a fix round is a gateway problem, not a
    code problem: it must not silently retry as if it were another fix try,
    and it must not crash the whole run as a bare AgentSystemError."""
    runtime = build_pipeline_runtime(
        tmp_path,
        reviewer_reply='{"verdict": "reject", "blockers": ["bug"]}',
        max_review_iterations=2,
        replies={"coder": "not json at all so the router still succeeds once"},
    )

    class FlakyCoderPipeline(RecordingPipeline):
        def _coder_fix(self, board, goal, *, error, history, override=None):
            raise AllProvidersFailed("coder", ["mock_coder/m -> rate_limit"])

    pipeline = FlakyCoderPipeline(runtime, board_path=tmp_path / "board.json")

    result = pipeline.run(GOAL)

    assert result.ok is False
    assert result.reason == "escalated"
    assert result.escalation["reason"] == "gateway_exhausted"
    # the loop stopped at the FIRST gateway failure, not after burning every try
    board = TaskBoard.load(result.board_path)
    assert board.records["coder"].attempts == 1  # only the initial (non-fix) call


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


# --------------------------------------------------------------------------- #
# Phase 3 + auto-checks: workspace execution inside the pipeline
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


def test_pipeline_writes_files_and_runs_auto_checks(tmp_path: Path) -> None:
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


def test_failing_test_drives_the_autocheck_fix_loop_before_review(tmp_path: Path) -> None:
    runtime = build_pipeline_runtime(
        tmp_path,
        stages=["coder", "tester", "reviewer"],
        max_autocheck_iterations=2,
        apply_workspace=True,
        run_tests=True,
    )
    pipeline = StubPipeline(
        runtime,
        board_path=tmp_path / "board.json",
        replies={
            "coder": [
                '{"files": [{"path": "check.py", "content": "import sys\\n\\nsys.exit(1)\\n"}]}',
                '{"files": [{"path": "check.py", "content": "print(\'fixed\')\\n"}]}',
            ],
            "tester": ['{"run_command": "python check.py"}'],
            "reviewer": ['{"verdict": "approve"}'],
        },
    )

    result = pipeline.run(GOAL)

    assert result.ok is True
    assert len(pipeline.stubs["coder"].messages) == 2  # the fix round ran
    assert len(pipeline.stubs["reviewer"].messages) == 1  # only after checks were clean
    assert result.tests["ok"] is True
    # the fix round got the exact failure, not a guess, and nothing else
    fix_context = pipeline.stubs["coder"].contexts[-1]
    assert set(fix_context) == {"code", "error", "history"}
    assert "FAILED" in fix_context["error"] or "failed" in fix_context["error"].lower()
    assert "auto-check attempt 1/2" in fix_context["history"]

    kinds = _events(tmp_path)
    assert "pipeline.autocheck_failed" in kinds


def test_autocheck_escalates_after_max_tries_instead_of_failing(tmp_path: Path) -> None:
    runtime = build_pipeline_runtime(
        tmp_path,
        stages=["coder", "tester", "reviewer", "devops"],
        max_autocheck_iterations=1,
        apply_workspace=True,
        run_tests=True,
    )
    failing = '{"files": [{"path": "check.py", "content": "import sys\\n\\nsys.exit(2)\\n"}]}'
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
    assert result.reason == "escalated"
    assert result.escalation["stage"] == "coder"
    assert result.escalation["reason"] == "test_failed"
    assert "check.py" in result.escalation["code"]
    board = TaskBoard.load(result.board_path)
    assert board.records["coder"].attempts == 2  # 1 initial + 1 allowed fix try
    # the reviewer never even ran: auto-checks gate it
    assert board.records["reviewer"].status == SKIPPED
    assert board.records["devops"].status == SKIPPED
    assert "pipeline.escalated" in _events(tmp_path)


def test_pipeline_skips_execution_when_there_is_no_test_command(tmp_path: Path) -> None:
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

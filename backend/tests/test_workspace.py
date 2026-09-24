"""Phase 3 tests: the sandbox, file application and command execution.

Everything here is offline and fast: real filesystem in ``tmp_path``, and only
harmless ``python -c`` commands (which are on the allowlist) as subprocesses.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from backend.core.orchestrator.board import TaskBoard
from backend.core.provider.registry import ExecutionConfig
from backend.core.workspace import (
    ApplyReport,
    CommandNotAllowed,
    CommandRunner,
    UnsafePath,
    Workspace,
    apply_board,
    detect_test_command,
    safe_relative_path,
    slugify,
)
from backend.core.workspace import test_command_for as workspace_test_command


# --------------------------------------------------------------------------- #
# sandbox: paths
# --------------------------------------------------------------------------- #
def test_safe_relative_path_normalises_ok_paths() -> None:
    assert safe_relative_path("src/main.py").as_posix() == "src/main.py"
    assert safe_relative_path("src\\main.py").as_posix() == "src/main.py"
    assert safe_relative_path("./a/./b.txt").as_posix() == "a/b.txt"
    assert safe_relative_path("  README.md  ").as_posix() == "README.md"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "../evil.py",
        "a/../../b.py",
        "/etc/passwd",
        "C:\\Windows\\System32\\evil.dll",
        "c:/windows/evil.exe",
        "a/\x00b.py",
        "nul",
        "CON.txt",
        "com1",
        "src/./con.py",
    ],
)
def test_safe_relative_path_rejects_escapes(raw: str) -> None:
    with pytest.raises(UnsafePath):
        safe_relative_path(raw)


def test_slugify_is_flat_and_safe() -> None:
    assert slugify("A Pomodoro Timer CLI in Python!") == "a-pomodoro-timer-cli-in-python"
    assert slugify("") == "project"
    assert "/" not in slugify("../../etc/passwd")
    assert len(slugify("x" * 200)) == 40


def test_workspace_writes_stay_inside_the_project(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    target = workspace.write_text("demo", "src/main.py", "print('hi')\n")

    assert target.read_text(encoding="utf-8") == "print('hi')\n"
    assert workspace.project_dir("demo") == tmp_path / "workspace" / "demo"
    with pytest.raises(UnsafePath):
        workspace.write_text("demo", "../outside.py", "nope")
    assert not (tmp_path / "outside.py").exists()
    # the project name itself is slugified, so it can never be a path escape
    assert workspace.project_dir("../../etc").name == "etc"


# --------------------------------------------------------------------------- #
# apply: board manifests -> real files
# --------------------------------------------------------------------------- #
def _board_with_files(
    tmp_path: Path, *, coder: list[dict], tester: dict | None = None
) -> TaskBoard:
    board = TaskBoard.new(
        run_id="r1", goal="g", stages=["coder", "tester"], agents={}, project="demo"
    )
    board.save(tmp_path / "board.json")
    board.start("coder", agent="coder")
    board.complete(
        "coder", text="{}", parsed={"files": coder}, target="mock/m", tokens=1, latency_ms=1
    )
    if tester is not None:
        board.start("tester", agent="tester")
        board.complete(
            "tester", text="{}", parsed=tester, target="mock/m", tokens=1, latency_ms=1
        )
    return board


def test_apply_writes_then_skips_unchanged_files(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    board = _board_with_files(
        tmp_path,
        coder=[
            {"path": "src/main.py", "content": "print('hi')\n"},
            {"path": "README.md", "content": "# hi\n"},
        ]
    )

    first = apply_board(board, workspace, "demo")
    assert first.counts() == {
        "total": 2,
        "written": 2,
        "overwritten": 0,
        "unchanged": 0,
        "rejected": 0,
    }
    assert (workspace.project_dir("demo") / "src" / "main.py").exists()

    second = apply_board(board, workspace, "demo")
    assert second.counts()["unchanged"] == 2
    assert second.changed == 0


def test_apply_overwrites_changed_content_and_reports_it(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    board = _board_with_files(tmp_path, coder=[{"path": "a.py", "content": "one\n"}])
    apply_board(board, workspace, "demo")

    board.records["coder"].parsed = {"files": [{"path": "a.py", "content": "two\n"}]}
    report = apply_board(board, workspace, "demo")

    assert report.counts()["overwritten"] == 1
    assert (workspace.project_dir("demo") / "a.py").read_text(encoding="utf-8") == "two\n"


def test_apply_rejects_unsafe_paths_and_notes_stage_conflicts(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    board = _board_with_files(
        tmp_path,
        coder=[
            {"path": "../escape.py", "content": "nope"},
            {"path": "shared.py", "content": "from coder\n"},
        ],
        tester={"files": [{"path": "shared.py", "content": "from tester\n"}]},
    )

    report = apply_board(board, workspace, "demo")

    assert len(report.rejected) == 1
    assert "escapes the project" in report.rejected[0].note
    assert not (tmp_path / "escape.py").exists()
    assert (workspace.project_dir("demo") / "shared.py").read_text(
        encoding="utf-8"
    ) == "from tester\n"
    conflict = [o for o in report.outcomes if o.path == "shared.py" and o.note]
    assert conflict and "later stage wins" in conflict[0].note


def test_apply_dry_run_writes_nothing(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    board = _board_with_files(tmp_path, coder=[{"path": "a.py", "content": "x\n"}])

    report = apply_board(board, workspace, "demo", dry_run=True)

    assert report.dry_run is True
    assert report.counts()["written"] == 1  # would be written
    assert not (workspace.root / "demo").exists()


def test_apply_ignores_stages_without_a_file_manifest(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    board = _board_with_files(tmp_path, coder=[{"path": "a.py", "content": "x\n"}])
    board.records["coder"].parsed = {"summary": "no files key at all"}

    report = apply_board(board, workspace, "demo")

    assert isinstance(report, ApplyReport)
    assert report.counts()["total"] == 0
    assert report.summary() == "no files were produced by the stages"


# --------------------------------------------------------------------------- #
# runner: allowlist, capture, timeout, truncation
# --------------------------------------------------------------------------- #
def _runner(**overrides) -> CommandRunner:
    config = ExecutionConfig(
        allow=["python", "py"],
        timeout_seconds=overrides.pop("timeout_seconds", 30),
        max_output_bytes=overrides.pop("max_output_bytes", 4000),
        env={"PYTHONIOENCODING": "utf-8"},
        **overrides,
    )
    return CommandRunner(config)


@pytest.mark.parametrize(
    "command",
    [
        "curl http://example.com",
        "powershell -Command Remove-Item -Recurse C:\\",
        "rm -rf /",
        "del /f /q C:\\Windows\\*",
        "python; rm -rf /",
        "",
    ],
)
def test_runner_refuses_commands_off_the_allowlist(command: str, tmp_path: Path) -> None:
    with pytest.raises(CommandNotAllowed):
        _runner().run(command, cwd=tmp_path)


def test_runner_captures_output_and_exit_code(tmp_path: Path) -> None:
    runner = _runner()

    ok = runner.run('python -c "print(\'hello from the sandbox\')"', cwd=tmp_path)
    assert ok.ok is True
    assert ok.exit_code == 0
    assert "hello from the sandbox" in ok.stdout
    # `python` is pinned to the running interpreter (the venv that has pytest)
    assert ok.argv[0] == sys.executable
    assert ok.argv[1:3] == ["-c", "print('hello from the sandbox')"]

    bad = runner.run("python -c \"import sys; sys.exit(3)\"", cwd=tmp_path)
    assert bad.ok is False
    assert bad.exit_code == 3
    assert bad.summary().startswith("exit 3")


def test_runner_stops_a_timed_out_command(tmp_path: Path) -> None:
    runner = _runner(timeout_seconds=1.5)

    result = runner.run('python -c "import time; time.sleep(60)"', cwd=tmp_path)

    assert result.timed_out is True
    assert result.ok is False
    assert result.duration_ms < 20_000  # it was killed, not waited out


def test_runner_truncates_huge_output(tmp_path: Path) -> None:
    runner = _runner(max_output_bytes=500)

    result = runner.run('python -c "print(\'x\' * 50000)"', cwd=tmp_path)

    assert result.ok is True
    assert result.stdout_truncated is True
    assert len(result.stdout) < 2000
    assert "characters truncated" in result.stdout


def test_runner_refuses_a_disabled_allowlist(tmp_path: Path) -> None:
    runner = CommandRunner(ExecutionConfig(enabled=False))
    with pytest.raises(CommandNotAllowed):
        runner.run('python -c "print(1)"', cwd=tmp_path)


# --------------------------------------------------------------------------- #
# which command to run
# --------------------------------------------------------------------------- #
def test_test_command_prefers_the_testers_own_command(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    board = TaskBoard.new(run_id="r", goal="g", stages=["tester"], agents={})
    board.save(tmp_path / "board.json")
    board.start("tester", agent="tester")
    board.complete(
        "tester",
        text="{}",
        parsed={"run_command": "python -m pytest -q"},
        target="mock/m",
        tokens=1,
        latency_ms=1,
    )

    assert workspace_test_command(board, tmp_path) == ("python -m pytest -q", "tester")


def test_test_command_is_detected_when_the_tester_is_silent(tmp_path: Path) -> None:
    board = TaskBoard.new(run_id="r", goal="g", stages=["tester"], agents={})
    board.save(tmp_path / "board.json")
    (tmp_path / "test_app.py").write_text("def test_ok(): pass\n", encoding="utf-8")

    assert workspace_test_command(board, tmp_path) == ("python -m pytest -q", "detected")
    assert workspace_test_command(board, tmp_path / "missing") == (None, "none")


def test_test_command_detects_node_projects(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    assert detect_test_command(tmp_path) == "npm test --silent"



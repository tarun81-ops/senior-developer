"""Run the generated project's own commands, strictly inside the sandbox.

Three rules keep this safe for a local dev tool:

  1. **No shell.** The command string is split into argv and executed with
     ``shell=False``, so ``&&``, ``|``, ``>`` and backticks are literal
     characters rather than operators - there is nothing for a model to inject
     into.
  2. **Allowlist by executable.** Only the programs listed under
     ``execution.allow`` in ``config/limits.yaml`` may start at all; ``rm``,
     ``curl``, ``powershell`` and friends are refused before a process exists.
  3. **Bounded.** Every run happens with ``cwd`` = the project folder, under a
     timeout that kills the process tree, with a scrubbed environment and
     captured output that is truncated (head + tail) so one runaway log cannot
     fill the board.

The runner never raises for a *failing* command - a non-zero exit code is a
result, and those results are what the fix loop feeds back to the coder.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.core.errors import ConfigError
from backend.core.provider.registry import ExecutionConfig

#: how much of the truncated output to keep from the front vs the back
_HEAD_SHARE = 0.6
_TAIL_SHARE = 0.3

#: How long a single ``communicate`` wait blocks before the runner re-checks
#: the cancel predicate. Small enough that a cancel feels immediate, large
#: enough not to spin.
_CANCEL_POLL_SECONDS = 0.25


class CommandNotAllowed(ConfigError):
    """The command is not allowed to run at all (disabled, bad syntax, not listed)."""


@dataclass
class CommandResult:
    """One executed command and everything worth knowing about it."""

    command: str
    argv: list[str]
    cwd: str
    exit_code: int
    ok: bool
    duration_ms: int
    timed_out: bool = False
    #: True when a cancel request killed the process tree (D20). The command did
    #: not "fail" on its own terms, so callers distinguish it from a non-zero exit.
    cancelled: bool = False
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    def summary(self) -> str:
        if self.cancelled:
            head = f"CANCELLED after {self.duration_ms} ms"
        elif self.timed_out:
            head = f"TIMEOUT after {self.duration_ms} ms"
        else:
            head = f"exit {self.exit_code} in {self.duration_ms} ms"
        return f"{head} ({'ok' if self.ok else 'failed'})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "exit_code": self.exit_code,
            "ok": self.ok,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CommandResult:
        return cls(
            command=str(raw.get("command") or ""),
            argv=[str(a) for a in raw.get("argv") or []],
            cwd=str(raw.get("cwd") or ""),
            exit_code=int(raw.get("exit_code") or 0),
            ok=bool(raw.get("ok")),
            duration_ms=int(raw.get("duration_ms") or 0),
            timed_out=bool(raw.get("timed_out")),
            cancelled=bool(raw.get("cancelled")),
            stdout=str(raw.get("stdout") or ""),
            stderr=str(raw.get("stderr") or ""),
            stdout_truncated=bool(raw.get("stdout_truncated")),
            stderr_truncated=bool(raw.get("stderr_truncated")),
        )

    def context_block(self, *, max_chars: int = 6000) -> str:
        """Compact text for a prompt: the failing evidence, not the whole log."""
        lines = [
            f"command: {self.command}",
            f"result: {self.summary()}",
        ]
        if self.stdout.strip():
            lines.append("stdout:\n" + self.stdout.strip())
        if self.stderr.strip():
            lines.append("stderr:\n" + self.stderr.strip())
        block = "\n".join(lines)
        if len(block) > max_chars:
            block = block[:max_chars] + "\n... [output cut for the prompt] ..."
        return block


def split_command(command: str) -> list[str]:
    """Split a command line into argv without ever invoking a shell."""
    text = (command or "").strip()
    if not text:
        raise CommandNotAllowed("empty command")
    try:
        raw = shlex.split(text, posix=False)
    except ValueError as exc:  # unbalanced quotes, etc.
        raise CommandNotAllowed(f"could not parse command: {exc}") from exc
    argv = []
    for token in raw:
        if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
            token = token[1:-1]
        if token:
            argv.append(token)
    if not argv:
        raise CommandNotAllowed(f"could not parse command: {command!r}")
    return argv


def executable_of(argv: list[str]) -> str:
    """The allowlist key for argv: the file name, lowered, without .exe/.cmd."""
    name = Path(argv[0]).name.lower()
    for suffix in (".exe", ".cmd", ".bat"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    """Keep the head and the tail: the first lines explain, the last ones judge."""
    if len(text) <= limit:
        return text, False
    head = int(limit * _HEAD_SHARE)
    tail = int(limit * _TAIL_SHARE)
    dropped = len(text) - head - tail
    return f"{text[:head]}\n... [{dropped} characters truncated] ...\n{text[-tail:]}", True


def _kill_tree(process: subprocess.Popen) -> None:
    """Kill the process and its children (a test runner spawns threads)."""
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
            )
        else:  # pragma: no cover - exercised on CI only
            process.kill()
    except OSError:  # pragma: no cover - already gone
        pass


class CommandRunner:
    """Executes allowlisted commands with a timeout and captured output."""

    def __init__(self, config: ExecutionConfig, *, cancel_check: CancelCheck | None = None) -> None:
        self.config = config
        #: Optional predicate polled while a command runs. When it turns true the
        #: process *and its whole tree* are killed (D20) — that is what makes
        #: "cancel this run" stop a test suite that spawns children, rather than
        #: leaving orphans behind. ``None`` (the CLI) means never cancel.
        self.cancel_check = cancel_check

    # -- policy -------------------------------------------------------------
    @property
    def allowed(self) -> list[str]:
        return sorted({name.lower() for name in self.config.allow})

    def check(self, command: str) -> list[str]:
        """Return argv for an allowed command, or raise :class:`CommandNotAllowed`."""
        if not self.config.enabled:
            raise CommandNotAllowed("command execution is disabled (execution.enabled: false)")
        argv = split_command(command)
        executable = executable_of(argv)
        if executable not in self.allowed:
            raise CommandNotAllowed(
                f"'{executable}' is not on the execution allowlist "
                f"({', '.join(self.allowed)})"
            )
        if executable in ("python", "py"):
            # Use the interpreter that is running us, not whatever PATH finds:
            # the generated tests need the same environment (pytest et al) that
            # this project was installed into.
            argv[0] = sys.executable
        return argv

    # -- execution ----------------------------------------------------------
    def run(self, command: str, *, cwd: Path | str) -> CommandResult:
        argv = self.check(command)
        cwd = Path(cwd)
        if not cwd.is_dir():
            raise CommandNotAllowed(f"working directory does not exist: {cwd}")

        env = os.environ.copy()
        env.update({str(key): str(value) for key, value in self.config.env.items()})
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0

        started = time.perf_counter()
        process = subprocess.Popen(  # noqa: S603 - argv list, shell=False, allowlisted
            argv,
            cwd=str(cwd),
            env=env,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        timed_out = False
        cancelled = False
        out, err, timed_out, cancelled = self._wait(process)
        duration_ms = int((time.perf_counter() - started) * 1000)

        stdout, out_cut = _truncate(out or "", self.config.max_output_bytes)
        stderr, err_cut = _truncate(err or "", self.config.max_output_bytes)
        exit_code = process.returncode if process.returncode is not None else -1
        return CommandResult(
            command=command,
            argv=argv,
            cwd=str(cwd),
            exit_code=exit_code,
            ok=exit_code == 0 and not timed_out and not cancelled,
            duration_ms=duration_ms,
            timed_out=timed_out,
            cancelled=cancelled,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=out_cut,
            stderr_truncated=err_cut,
        )

    def _wait(self, process: subprocess.Popen) -> tuple[str, str, bool, bool]:
        """Wait for the command, honouring both the timeout and cancellation.

        One long ``communicate`` call would sit blind until the process ended, so
        a cancel request could not stop a command that outlives it. Instead the
        wait is taken in short slices: each timeout is a chance to ask the
        ``cancel_check`` predicate, and the same tree-killing path used for a
        timeout handles a cancel — on Windows that is ``taskkill /T /F``, which
        takes the grandchildren with it (D20).

        Returns ``(stdout, stderr, timed_out, cancelled)``.
        """
        deadline = time.monotonic() + self.config.timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_tree(process)
                return (*self._drain(process), True, False)
            try:
                out, err = process.communicate(timeout=min(_CANCEL_POLL_SECONDS, remaining))
                return out or "", err or "", False, False
            except subprocess.TimeoutExpired:
                if self.cancel_check is not None and self.cancel_check():
                    _kill_tree(process)
                    return (*self._drain(process), False, True)

    @staticmethod
    def _drain(process: subprocess.Popen) -> tuple[str, str]:
        """Collect whatever the killed process managed to print."""
        try:
            out, err = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - stubborn child
            return "", ""
        return out or "", err or ""


def detect_test_command(project_dir: Path | str) -> str | None:
    """A sensible test command for whatever is actually on disk."""
    project_dir = Path(project_dir)
    if (project_dir / "package.json").exists():
        return "npm test --silent"
    has_python_tests = (
        (project_dir / "tests").is_dir()
        or any(project_dir.glob("test_*.py"))
        or any(project_dir.glob("*_test.py"))
    )
    return "python -m pytest -q" if has_python_tests else None


def test_command_for(board, project_dir: Path | str) -> tuple[str | None, str]:
    """``(command, source)``: the tester's own run_command, else detection."""
    from backend.core.orchestrator.board import DONE

    record = board.records.get("tester")
    if record is not None and record.status == DONE and isinstance(record.parsed, dict):
        command = str(record.parsed.get("run_command") or "").strip()
        if command:
            return command, "tester"
    detected = detect_test_command(project_dir)
    return (detected, "detected") if detected else (None, "none")


"""Core terminal-session logic (Part B, terminal tab; D43): a real
``powershell.exe``, driven directly, with no HTTP layer in between.

This is a different trust boundary from ``CommandRunner``'s (see
``backend/core/workspace/runner.py`` and ``terminal.py``'s module docstring):
there is no allowlist here on purpose, because this is the *person's* own
shell, not something a model's output can reach. These tests spawn a real
PowerShell process, so — like the junction test in ``test_api_files.py`` —
they only run on Windows.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from backend.core.workspace.terminal import TerminalBusy, TerminalRecord

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="spawns a real powershell.exe")

#: Generous but bounded: CI machines and a first PowerShell cold-start can be
#: slow, but nothing here should ever take anywhere near this long.
_TIMEOUT = 20.0


def _wait_until(predicate, *, timeout: float = _TIMEOUT, step: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


def _run_and_collect(record: TerminalRecord, command: str, *, timeout: float = _TIMEOUT) -> tuple[str, bool | None]:
    """Submit one command and collect its output text and ``$?`` up to its
    ``done`` marker. Fails the test instead of hanging forever if it never
    arrives."""
    cursor = record.last_seq
    record.submit(command)
    text = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for chunk in record.replay_and_wait(cursor, timeout=1.0):
            cursor = chunk.seq
            text += chunk.text
            if chunk.done:
                return text, chunk.ok
    pytest.fail(f"command {command!r} never finished; output so far: {text!r}")


@pytest.fixture
def record(tmp_path: Path) -> TerminalRecord:
    rec = TerminalRecord(tmp_path)
    yield rec
    rec.close()


def test_a_command_runs_in_the_given_folder_and_reports_ok(record: TerminalRecord, tmp_path: Path) -> None:
    text, ok = _run_and_collect(record, "Write-Output (Get-Location).Path")
    assert str(tmp_path) in text
    assert ok is True


def test_a_failing_command_is_reported_not_ok(record: TerminalRecord) -> None:
    _text, ok = _run_and_collect(record, "Get-Item nope-does-not-exist.txt -ErrorAction Stop")
    assert ok is False


def test_state_persists_between_commands(record: TerminalRecord) -> None:
    """A real shell, not one process per command: variables (and cwd) survive."""
    _run_and_collect(record, "$sda_test = 41 + 1")
    text, ok = _run_and_collect(record, "Write-Output $sda_test")
    assert "42" in text
    assert ok is True


def test_a_second_command_is_rejected_while_the_first_is_busy(record: TerminalRecord) -> None:
    record.submit("Start-Sleep -Seconds 2")
    assert record.busy is True
    with pytest.raises(TerminalBusy):
        record.submit("Write-Output 'too soon'")
    assert _wait_until(lambda: not record.busy)


def test_restart_kills_the_shell_and_a_new_command_still_works(record: TerminalRecord) -> None:
    record.submit("Start-Sleep -Seconds 30")
    assert record.busy is True
    record.close()
    assert _wait_until(lambda: record.closed)
    text, ok = _run_and_collect(record, "Write-Output 'back again'")
    assert "back again" in text
    assert ok is True


def test_sequence_numbers_never_reset_across_a_restart(record: TerminalRecord) -> None:
    _run_and_collect(record, "Write-Output one")
    before = record.last_seq
    record.close()
    assert _wait_until(lambda: record.closed)
    _run_and_collect(record, "Write-Output two")
    assert record.last_seq > before

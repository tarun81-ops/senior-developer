"""An interactive PowerShell session per run, for a person to poke at the
generated project (Part B, terminal tab; D43).

This is a **second, deliberately separate** execution path from
:mod:`backend.core.workspace.runner`. :class:`~.runner.CommandRunner` is what
the *agents* use to run a command: argv-only, ``shell=False``, allowlisted by
executable, because a model's output is untrusted input that must never reach
a real shell (see that module's docstring). Nothing here changes that — the
allowlist still refuses ``powershell`` for an agent, exactly as before.

This module is for the *person* at the keyboard instead. They already have a
real PowerShell window and full access to their own machine, so there is
nothing to sandbox about *what* they can run — that would be security
theatre. What this bounds is only *where* it starts:

* one real ``powershell.exe``, per run, with its **cwd** fixed to that run's
  project folder under ``workspace/``;
* exactly one session per run, started the first time it is needed and
  killed on API shutdown, so a forgotten terminal tab never leaves a
  ``powershell.exe`` running after the app closes;
* output is buffered **in memory only**, replayable by sequence number (the
  same shape as the SQLite event log in :mod:`backend.api.event_store`, so
  the UI's reconnect-and-resume logic works the same way) but never written
  to disk — a terminal session does not outlive the API process, unlike a
  run's own event history (D19).

Because there is no real pseudo-terminal here (no ANSI colour, no in-place
progress bars, no ``Read-Host`` for interactive prompts), a command is
submitted as a single line and its output streams back as plain text. A
command that never exits (a dev server, ``ping -t``) simply keeps the
session busy until it is interrupted or the shell is restarted — the same
as it would in a real terminal.
"""

from __future__ import annotations

import codecs
import os
import signal
import subprocess
import sys
import threading
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

#: Output kept per run, in characters. Old lines are dropped once this is
#: exceeded, the same "head/tail bounded" spirit as CommandRunner's output cap
#: — a terminal left open for hours must not grow without limit.
MAX_BUFFERED_CHARS = 400_000

#: A command line longer than this is almost certainly pasted, not typed.
MAX_COMMAND_CHARS = 4000


class TerminalBusy(RuntimeError):
    """A command is already running in this session."""


class TerminalStartError(RuntimeError):
    """The shell process could not be started at all."""


@dataclass(frozen=True)
class TerminalChunk:
    """One piece of terminal activity: output text, or a status marker.

    ``done`` closes out the command that was running (``ok`` is a best-effort
    signal from PowerShell's ``$?``, not a real exit code — see
    :meth:`_ShellProcess._drain_lines`). ``closed`` means the shell process
    itself has exited; no more chunks will arrive from this session, though a
    new command will transparently start a fresh one.
    """

    seq: int
    text: str
    done: bool = False
    ok: bool | None = None
    closed: bool = False

    def to_dict(self) -> dict:
        return {"seq": self.seq, "text": self.text, "done": self.done, "ok": self.ok, "closed": self.closed}


def _kill_tree(process: subprocess.Popen) -> None:
    """Kill the process and anything it started (mirrors CommandRunner's helper)."""
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
            )
        else:  # pragma: no cover - this app targets Windows
            process.kill()
    except OSError:  # pragma: no cover - already gone
        pass


class _ShellProcess:
    """One live ``powershell.exe``, fed one line at a time over its stdin.

    There is no pseudo-terminal, so PowerShell does not know it is
    "interactive" — it reads each stdin line as a script statement and
    writes results to stdout, with stderr merged into the same stream so
    output interleaves the way it would in a real console. A marker line is
    appended after every submitted command so the reader thread can tell
    where that command's output ends, since nothing else in a plain pipe
    marks a boundary.
    """

    def __init__(self, cwd: Path, *, on_output: Callable[..., None]) -> None:
        self._on_output = on_output
        self._marker = f"@@SDA-TERM-{uuid.uuid4().hex[:12]}@@"
        self._state_lock = threading.Lock()
        self._busy = False
        self.exited = False

        env = os.environ.copy()
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        try:
            self._process = subprocess.Popen(  # noqa: S603 - fixed argv, shell=False
                ["powershell.exe", "-NoLogo", "-NoProfile"],
                cwd=str(cwd),
                env=env,
                shell=False,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise TerminalStartError(f"could not start PowerShell: {exc}") from exc

        self._reader = threading.Thread(target=self._read_loop, daemon=True, name="sda-terminal")
        self._reader.start()

    @property
    def busy(self) -> bool:
        with self._state_lock:
            return self._busy

    def write(self, command: str) -> None:
        with self._state_lock:
            if self._busy:
                raise TerminalBusy()
            self._busy = True
        # One line for the command, one for the marker: PowerShell executes
        # stdin as a script, statement by statement, so both run in order in
        # the same session (cwd, variables and env survive between calls).
        # `$?` reflects whether the line just run reported success — the
        # closest thing to an exit code available for both cmdlets and
        # native commands without a real console attached.
        marker_line = f'Write-Output "{self._marker}:$(if ($?) {{0}} else {{1}})"'
        payload = f"{command}\r\n{marker_line}\r\n".encode("utf-8", errors="replace")
        try:
            self._process.stdin.write(payload)
            self._process.stdin.flush()
        except (BrokenPipeError, OSError):
            with self._state_lock:
                self._busy = False
            raise

    def interrupt(self) -> None:
        """Best-effort Ctrl+Break: PowerShell treats it like Ctrl+C for a
        running pipeline. Not guaranteed for every command, but enough to
        unstick a hung foreground command without killing the session."""
        if sys.platform != "win32" or self._process.poll() is not None:
            return
        try:
            self._process.send_signal(signal.CTRL_BREAK_EVENT)
        except (OSError, ValueError):
            pass

    def kill(self) -> None:
        if self._process.poll() is None:
            _kill_tree(self._process)

    # -- the background reader ------------------------------------------------
    def _read_loop(self) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending = ""
        stdout = self._process.stdout
        try:
            while True:
                raw = stdout.read(4096)
                if not raw:
                    break
                pending = self._drain_lines(pending + decoder.decode(raw))
        finally:
            tail = pending + decoder.decode(b"", final=True)
            if tail:
                self._on_output(tail)
            self.exited = True
            with self._state_lock:
                self._busy = False
            code = self._process.poll()
            self._on_output(f"\n[PowerShell exited, code {code}]\n", closed=True)

    def _drain_lines(self, pending: str) -> str:
        """Emit each complete line, recognising the marker; return the rest."""
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            stripped = line[:-1] if line.endswith("\r") else line
            if stripped.startswith(self._marker):
                ok = stripped[len(self._marker) + 1 :].strip() == "0"
                with self._state_lock:
                    self._busy = False
                self._on_output("", done=True, ok=ok)
            else:
                self._on_output(line + "\n")
        return pending


class TerminalRecord:
    """One run's terminal: the output buffer, replayable by sequence number,
    and whichever :class:`_ShellProcess` is currently backing it.

    The buffer outlives any single shell process, so restarting a stuck
    session (or a command that typed ``exit``) never resets the sequence
    numbers a client has already seen — a reconnect just keeps asking for
    ``seq > cursor`` exactly as it did before.
    """

    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._chunks: deque[TerminalChunk] = deque()
        self._buffered_chars = 0
        self._next_seq = 1
        self._session: _ShellProcess | None = None
        self._start_error: str | None = None

    # -- state ---------------------------------------------------------------
    @property
    def busy(self) -> bool:
        with self._lock:
            return self._session.busy if self._session is not None else False

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._session is None or self._session.exited

    @property
    def last_seq(self) -> int:
        with self._cv:
            return self._chunks[-1].seq if self._chunks else 0

    @property
    def start_error(self) -> str | None:
        with self._lock:
            return self._start_error

    # -- lifecycle -------------------------------------------------------------
    def _ensure_started_locked(self) -> _ShellProcess:
        if self._session is None or self._session.exited:
            try:
                self._session = _ShellProcess(self.cwd, on_output=self._append)
                self._start_error = None
            except TerminalStartError as exc:
                self._start_error = str(exc)
                raise
        return self._session

    def submit(self, command: str) -> None:
        with self._lock:
            session = self._ensure_started_locked()
            session.write(command)

    def interrupt(self) -> None:
        with self._lock:
            if self._session is not None:
                self._session.interrupt()

    def close(self) -> None:
        """Kill the current shell, if any. The next command starts a fresh one."""
        with self._lock:
            if self._session is not None:
                self._session.kill()

    # -- buffered output -------------------------------------------------------
    def _append(self, text: str, *, done: bool = False, ok: bool | None = None, closed: bool = False) -> None:
        with self._cv:
            chunk = TerminalChunk(seq=self._next_seq, text=text, done=done, ok=ok, closed=closed)
            self._next_seq += 1
            self._chunks.append(chunk)
            self._buffered_chars += len(text)
            while self._buffered_chars > MAX_BUFFERED_CHARS and len(self._chunks) > 1:
                dropped = self._chunks.popleft()
                self._buffered_chars -= len(dropped.text)
            self._cv.notify_all()

    def replay_and_wait(self, after_seq: int, *, timeout: float) -> list[TerminalChunk]:
        """Chunks after ``after_seq``, waiting up to ``timeout`` for one if none
        are buffered yet. Always returns promptly (possibly empty) — the
        stream endpoint uses an empty result as its heartbeat cadence."""
        with self._cv:
            page = [c for c in self._chunks if c.seq > after_seq]
            if page:
                return page
            self._cv.wait(timeout=timeout)
            return [c for c in self._chunks if c.seq > after_seq]


class TerminalManager:
    """One :class:`TerminalRecord` per run, for the life of the API process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, TerminalRecord] = {}

    def get_or_create(self, run_id: str, cwd: Path) -> TerminalRecord:
        with self._lock:
            record = self._records.get(run_id)
            if record is None:
                record = TerminalRecord(cwd)
                self._records[run_id] = record
            return record

    def get(self, run_id: str) -> TerminalRecord | None:
        with self._lock:
            return self._records.get(run_id)

    def shutdown(self) -> None:
        """Kill every live shell. Called from the app's lifespan shutdown,
        same as :meth:`RunManager.shutdown` does for run commands."""
        with self._lock:
            records = list(self._records.values())
        for record in records:
            record.close()

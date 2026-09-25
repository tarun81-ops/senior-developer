"""Read-only access to one run's project folder, for the UI's file viewer (D30).

Everything here is bounded and stays inside ``workspace/<project>/``:

* a path is checked with :func:`safe_relative_path` (no absolute paths, no
  ``..``, no device names), then *resolved* and confirmed to still be under
  the project folder, so a symlink or junction pointing outside is refused;
* a listing skips tool/dependency folders and stops at :data:`MAX_LISTED_FILES`;
* a read returns at most :data:`MAX_VIEW_BYTES`, and binary files are
  reported as binary instead of being decoded.

Nothing here writes, and nothing takes a path that isn't relative to the run's
own project folder.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from backend.core.workspace.sandbox import UnsafePath, safe_relative_path

MAX_LISTED_FILES = 500
MAX_VIEW_BYTES = 256 * 1024

#: Folders that are tooling or dependencies, not the generated project.
SKIPPED_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
})


@dataclass(frozen=True)
class FileContent:
    path: str
    size: int
    content: str | None
    binary: bool
    truncated: bool


def _inside(base: Path, target: Path) -> bool:
    return target == base or base in target.parents


def list_files(project_dir: Path) -> tuple[list[tuple[str, int]], bool]:
    """``([(relative posix path, size), ...], truncated)``, sorted by path."""
    base = project_dir.resolve()
    if not base.is_dir():
        return [], False
    found: list[tuple[str, int]] = []
    for folder, dirs, names in os.walk(base):  # never follows directory links
        dirs[:] = sorted(d for d in dirs if d not in SKIPPED_DIRS)
        for name in sorted(names):
            path = Path(folder) / name
            try:
                if not _inside(base, path.resolve()) or not path.is_file():
                    continue  # a link that points outside, or not a regular file
                size = path.stat().st_size
            except OSError:
                continue
            found.append((path.relative_to(base).as_posix(), size))
            if len(found) >= MAX_LISTED_FILES:
                return sorted(found), True
    return sorted(found), False


def read_file(project_dir: Path, relative: str) -> FileContent:
    """One file's text. Raises :class:`UnsafePath` (400) or ``FileNotFoundError`` (404)."""
    base = project_dir.resolve()
    target = (base / safe_relative_path(relative)).resolve()
    if not _inside(base, target) or target == base:
        raise UnsafePath(f"path leaves the project: {relative!r}")
    if not target.is_file():
        raise FileNotFoundError(relative)
    size = target.stat().st_size
    with target.open("rb") as handle:
        data = handle.read(MAX_VIEW_BYTES + 1)
    truncated = len(data) > MAX_VIEW_BYTES
    data = data[:MAX_VIEW_BYTES]
    shown = target.relative_to(base).as_posix()
    text = None if b"\x00" in data else _decode(data, truncated)
    return FileContent(
        path=shown, size=size, content=text, binary=text is None, truncated=truncated
    )


def _decode(data: bytes, truncated: bool) -> str | None:
    """UTF-8 text, or ``None`` for binary. A cut may split one character."""
    for cut in range(4 if truncated else 1):
        try:
            return data[: len(data) - cut].decode("utf-8")
        except UnicodeDecodeError:
            continue
    return None

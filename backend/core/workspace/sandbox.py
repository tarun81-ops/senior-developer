"""The sandbox: every byte the agents write lands inside ``workspace/``.

A path produced by a language model is untrusted input, so before anything is
written we:

  * reject absolute paths and Windows drive letters (``C:\\...``, ``/etc/...``)
  * reject every ``..`` segment, so no path can climb out of the project
  * reject NUL bytes, empty segments and Windows device names (``CON``, ``NUL``)
  * re-resolve the final path and confirm it is still under the project root

That last check is the belt-and-braces one: even if a new Windows path quirk
appears, the resolved target must still be inside the sandbox.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: Windows device names: writing to these can hang or hit a device.
_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

_DRIVE = re.compile(r"^[A-Za-z]:")
_SLUG = re.compile(r"[^a-z0-9]+")


class UnsafePath(ValueError):
    """A path from a model (or a caller) tried to leave the sandbox."""


def safe_relative_path(raw: object) -> Path:
    """Normalise a model-supplied path, or raise :class:`UnsafePath`.

    Accepts ``src/main.py`` and ``src\\main.py``; refuses anything absolute or
    any attempt to walk upwards.
    """
    text = str(raw or "").strip().replace("\\", "/")
    if not text:
        raise UnsafePath("empty path")
    if "\x00" in text:
        raise UnsafePath("path contains a NUL byte")
    if text.startswith("/") or _DRIVE.match(text):
        raise UnsafePath(f"absolute paths are not allowed: {raw!r}")

    parts: list[str] = []
    for segment in text.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            raise UnsafePath(f"path escapes the project: {raw!r}")
        name = segment.rstrip(" .")  # Windows silently strips these; we refuse instead
        if not name:
            raise UnsafePath(f"invalid path segment in {raw!r}")
        if name.split(".")[0].lower() in _RESERVED:
            raise UnsafePath(f"reserved device name is not allowed: {raw!r}")
        parts.append(name)
    if not parts:
        raise UnsafePath(f"path has no usable segments: {raw!r}")
    return Path(*parts)


def slugify(text: str, *, max_length: int = 40) -> str:
    """Turn a goal or project name into one flat, filesystem-safe folder name."""
    slug = _SLUG.sub("-", (text or "").lower()).strip("-")
    slug = slug[:max_length].rstrip("-")
    return slug or "project"


@dataclass
class Workspace:
    """The only place generated code is allowed to exist."""

    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root)

    # -- folders ------------------------------------------------------------
    def project_dir(self, project: str, *, create: bool = True) -> Path:
        """``workspace/<slug>/`` — one flat folder per project, never nested."""
        path = self.root / slugify(project)
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def projects(self) -> list[Path]:
        if not self.root.exists():
            return []
        return sorted(p for p in self.root.iterdir() if p.is_dir())

    # -- paths --------------------------------------------------------------
    def resolve(self, project: str, relative: object) -> Path:
        """Resolve a project-relative path, refusing anything outside it."""
        base = self.project_dir(project).resolve()
        target = (base / safe_relative_path(relative)).resolve()
        if target != base and base not in target.parents:
            raise UnsafePath(f"resolved path leaves the project: {relative!r}")
        return target

    def read_text(self, project: str, relative: object) -> str:
        return self.resolve(project, relative).read_text(encoding="utf-8")

    def write_text(self, project: str, relative: object, content: str) -> Path:
        """Write one file (atomically: temp file, then replace)."""
        target = self.resolve(project, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(content, encoding="utf-8", newline="\n")
        tmp.replace(target)
        return target


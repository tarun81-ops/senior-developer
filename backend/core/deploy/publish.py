"""What leaves the machine when a project is deployed, and what stops it (D37).

Only files inside the run's own ``workspace/<project>/`` are ever considered.
From those:

* **excluded** (listed with a reason, never uploaded): environment files,
  private-key files, tool and dependency folders, caches, oversized files,
  and anything that resolves outside the project;
* **scanned**: every remaining text file is checked for strings shaped like a
  real credential. **Any hit blocks the deploy.** A finding names the file,
  the line and the kind of key, never the value, so the report can be shown
  and logged safely.

Pure local code: nothing here talks to the network.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path

from backend.core.deploy.detect import IGNORED_DIRS

#: Largest single file published. Generated projects are source code; a
#: bigger file is almost certainly an artifact or a data dump.
MAX_FILE_BYTES = 1024 * 1024
#: Largest total, a guard against publishing a whole disk by accident.
MAX_TOTAL_BYTES = 25 * 1024 * 1024

#: Files that are secrets by name, whatever they contain.
_SECRET_NAMES = (
    ".env",
    ".env.*",
    "*.env",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.keystore",
    "id_rsa",
    "id_rsa.*",
    "id_ed25519",
    "id_ed25519.*",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "credentials.json",
    "service-account*.json",
)
#: ...except templates meant to be committed.
_ALLOWED_NAMES = (".env.example", ".env.sample", ".env.template")

#: Key shapes. Each is specific enough that a hit is worth stopping for.
_KEY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Google API key", re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    ("OpenAI/OpenRouter key", re.compile(r"\bsk-(?:or-v1-|proj-)?[A-Za-z0-9_\-]{20,}")),
    ("Groq key", re.compile(r"\bgsk_[A-Za-z0-9]{20,}")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})")),
    ("Render API key", re.compile(r"\brnd_[A-Za-z0-9]{20,}")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("Private key block", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")),
)


@dataclass(frozen=True)
class Finding:
    """A likely credential. Never carries the matched text."""

    path: str
    line: int
    kind: str


@dataclass
class PublishSet:
    """What a deploy would publish. ``blocked`` means it must not happen."""

    files: list[tuple[str, int]] = field(default_factory=list)  # (relative path, size)
    excluded: list[tuple[str, str]] = field(default_factory=list)  # (relative path, why)
    findings: list[Finding] = field(default_factory=list)
    total_bytes: int = 0
    problem: str | None = None  # a reason that blocks the deploy, other than findings

    @property
    def blocked(self) -> bool:
        return bool(self.findings) or self.problem is not None


def build_publish_set(project_dir: Path) -> PublishSet:
    """Select and scan the files of one project folder."""
    root = Path(project_dir).resolve()
    result = PublishSet()
    if not root.is_dir():
        result.problem = "the project folder does not exist"
        return result
    for path in sorted(root.rglob("*")):
        rel_parts = path.relative_to(root).parts
        rel = "/".join(rel_parts)
        if any(part in IGNORED_DIRS for part in rel_parts[:-1]):
            continue  # a whole tool/dependency folder; not listed file by file
        if not path.is_file():
            continue
        why = _exclusion(path, root)
        if why:
            result.excluded.append((rel, why))
            continue
        size = path.stat().st_size
        result.files.append((rel, size))
        result.total_bytes += size
        result.findings.extend(_scan(path, rel))
    if not result.files:
        result.problem = "no files would be published"
    elif result.total_bytes > MAX_TOTAL_BYTES:
        result.problem = (
            f"the project is {result.total_bytes // (1024 * 1024)} MB; "
            f"the limit is {MAX_TOTAL_BYTES // (1024 * 1024)} MB"
        )
    return result


def _exclusion(path: Path, root: Path) -> str | None:
    name = path.name
    if not (path.resolve() == root or root in path.resolve().parents):
        return "points outside the project"
    if name not in _ALLOWED_NAMES and any(fnmatch.fnmatch(name, p) for p in _SECRET_NAMES):
        return "a secrets file by its name"
    if path.suffix in (".pyc", ".pyo") or name in (".DS_Store", "Thumbs.db"):
        return "a cache or OS file"
    if path.stat().st_size > MAX_FILE_BYTES:
        return f"larger than {MAX_FILE_BYTES // 1024} KB"
    return None


def _scan(path: Path, rel: str) -> list[Finding]:
    try:
        data = path.read_bytes()
    except OSError:
        return []
    if b"\x00" in data[:8192]:
        return []  # binary: key shapes in images or fonts are noise
    text = data.decode("utf-8", errors="replace")
    found: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in _KEY_PATTERNS:
            if pattern.search(line):
                found.append(Finding(path=rel, line=number, kind=kind))
    return found

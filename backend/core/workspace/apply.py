"""Materialise a board's file manifests into the workspace (Phase 3).

Coder, tester, devops and docs all emit ``{"files": [{"path", "content"}, ...]}``.
This module turns those JSON manifests into real files and reports exactly what
happened per file:

    written      a new file was created
    overwritten  an existing file was replaced with different content
    unchanged    identical content was already there (the common re-run case)
    rejected     unsafe path (see ``sandbox.safe_relative_path``) - never written

Later stages win a path conflict (devops' requirements.txt beats an earlier
draft from the coder) and the conflict is recorded in the report, never silent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from backend.core.orchestrator.board import DONE, TaskBoard
from backend.core.workspace.sandbox import UnsafePath, Workspace, safe_relative_path

#: Stages whose JSON output may contain a `files` manifest, in priority order.
FILE_STAGES: tuple[str, ...] = ("coder", "tester", "devops", "docs")

WRITTEN = "written"
OVERWRITTEN = "overwritten"
UNCHANGED = "unchanged"
REJECTED = "rejected"


@dataclass
class FileOutcome:
    """What happened to one path during one apply."""

    path: str
    status: str
    stage: str = ""
    size: int = 0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "status": self.status,
            "stage": self.stage,
            "size": self.size,
            "note": self.note,
        }


@dataclass
class ApplyReport:
    """The story of one ``apply_board`` call."""

    project: str
    root: str
    outcomes: list[FileOutcome] = field(default_factory=list)
    dry_run: bool = False

    def _with(self, status: str) -> list[FileOutcome]:
        return [o for o in self.outcomes if o.status == status]

    @property
    def written(self) -> list[FileOutcome]:
        return self._with(WRITTEN)

    @property
    def overwritten(self) -> list[FileOutcome]:
        return self._with(OVERWRITTEN)

    @property
    def unchanged(self) -> list[FileOutcome]:
        return self._with(UNCHANGED)

    @property
    def rejected(self) -> list[FileOutcome]:
        return self._with(REJECTED)

    @property
    def changed(self) -> int:
        """Files that were (or would be) created or replaced."""
        return len(self.written) + len(self.overwritten)

    def counts(self) -> dict[str, int]:
        return {
            "total": len(self.outcomes),
            "written": len(self.written),
            "overwritten": len(self.overwritten),
            "unchanged": len(self.unchanged),
            "rejected": len(self.rejected),
        }

    def summary(self) -> str:
        counts = self.counts()
        if not counts["total"]:
            return "no files were produced by the stages"
        parts = [f"{counts['written']} new", f"{counts['overwritten']} updated", f"{counts['unchanged']} unchanged"]
        if counts["rejected"]:
            parts.append(f"{counts['rejected']} REJECTED")
        prefix = "dry run: " if self.dry_run else ""
        return f"{prefix}{counts['total']} file(s) ({', '.join(parts)})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "root": self.root,
            "dry_run": self.dry_run,
            "counts": self.counts(),
            "outcomes": [o.to_dict() for o in self.outcomes],
        }


def _content_of(item: dict[str, Any]) -> str:
    """Coerce a manifest's ``content`` into text without losing information."""
    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # some models emit one string per line
        return "\n".join(str(part) for part in content)
    if content is None:
        return ""
    return str(content)


def collect_entries(
    board: TaskBoard, stages: Iterable[str] = FILE_STAGES
) -> list[tuple[str, dict[str, Any]]]:
    """Every ``(stage, file_entry)`` a board produced, in application order."""
    entries: list[tuple[str, dict[str, Any]]] = []
    for stage in stages:
        record = board.records.get(stage)
        if record is None or record.status != DONE or not isinstance(record.parsed, dict):
            continue
        files = record.parsed.get("files")
        if not isinstance(files, list):
            continue
        for item in files:
            if isinstance(item, dict) and str(item.get("path") or "").strip():
                entries.append((stage, item))
    return entries


def apply_board(
    board: TaskBoard,
    workspace: Workspace,
    project: str,
    *,
    stages: Iterable[str] = FILE_STAGES,
    dry_run: bool = False,
) -> ApplyReport:
    """Write every manifest in the board into ``workspace/<project>/``.

    ``dry_run`` reports what would change without touching the filesystem, which
    is what the CLI uses for ``--dry-run``.
    """
    report = ApplyReport(
        project=project,
        root=str(workspace.root / project),
        dry_run=dry_run,
    )
    seen: dict[str, str] = {}
    for stage, item in collect_entries(board, stages):
        raw_path = str(item.get("path"))
        content = _content_of(item)
        try:
            relative = safe_relative_path(raw_path)
        except UnsafePath as exc:
            report.outcomes.append(
                FileOutcome(path=raw_path, status=REJECTED, stage=stage, note=str(exc))
            )
            continue

        key = relative.as_posix()
        note = ""
        if key in seen:
            note = f"also emitted by '{seen[key]}'; the later stage wins"
        seen[key] = stage

        if dry_run:
            target = workspace.project_dir(project, create=False) / relative
        else:
            target = workspace.resolve(project, relative)

        if target.exists():
            try:
                existing: str | None = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                existing = None  # binary or unreadable: treat as different
            status = UNCHANGED if existing == content else OVERWRITTEN
        else:
            status = WRITTEN

        if status != UNCHANGED and not dry_run:
            workspace.write_text(project, relative, content)

        report.outcomes.append(
            FileOutcome(
                path=key,
                status=status,
                stage=stage,
                size=len(content.encode("utf-8")),
                note=note,
            )
        )
    return report


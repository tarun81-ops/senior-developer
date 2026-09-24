"""Pydantic request/response models for the HTTP API.

Conventions used throughout (matching ``backend/core/provider/schemas.py``):
Pydantic v2 models, ``snake_case`` field names that mirror the JSON, ``Field``
constraints for every user-supplied string/list, and ``model_config`` set
explicitly rather than relying on defaults.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    """``GET /api/health`` — enough for a launcher to know the server is up."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    version: str
    api_host: Literal["127.0.0.1"] = "127.0.0.1"
    #: The port the server actually bound, so a launcher that asked for port 0
    #: (ephemeral) can still tell the shell where to connect.
    api_port: int
    #: Literal "ok" because reaching this line means the token was accepted.
    auth: Literal["ok"] = "ok"


class EventResponse(BaseModel):
    """One event as the UI receives it (SSE payload and replay page)."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    seq: int = Field(..., ge=0, description="Per-run sequence; pass back as after_seq")
    ts: str
    kind: str
    message: str
    agent: str | None = None
    provider: str | None = None
    model: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class EventsPageResponse(BaseModel):
    """A replay page: ``events`` plus the cursor to resume from."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    events: list[EventResponse]
    last_seq: int = Field(..., description="Highest seq in this page; -1 when empty")
    has_more: bool = Field(..., description="True when more events exist after last_seq")


# -- run lifecycle ------------------------------------------------------------
#: Lifecycle states for a run created through the API. ``waiting_approval`` is
#: used from Part A step 3; it is part of the type now so the UI can be built
#: against the final shape.
RunStatus = Literal[
    "queued",
    "running",
    "waiting_approval",
    "succeeded",
    "failed",
    "cancelled",
]

#: Statuses from which no further transition happens.
TERMINAL_STATUSES: frozenset[str] = frozenset({"succeeded", "failed", "cancelled"})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class CreateRunRequest(BaseModel):
    """``POST /api/runs`` body — the same options the CLI's ``build`` takes."""

    model_config = ConfigDict(extra="forbid")

    request: str = Field(
        ...,
        min_length=1,
        max_length=4000,
        description="What to build; the only free text the API accepts.",
    )
    project: str | None = Field(
        default=None,
        min_length=1,
        max_length=60,
        description="Folder name under workspace/; slugified exactly like the CLI.",
    )
    dry_run: bool = Field(
        default=False, description="Plan only: report the file writes, do not write them"
    )
    no_apply: bool = Field(
        default=False, description="Do not write generated files into workspace/"
    )
    no_run_tests: bool = Field(
        default=False, description="Do not execute the tester's command"
    )
    override: list[list[str]] | None = Field(
        default=None,
        description="Per-agent model override as [agent, provider, model] triples.",
    )
    stages: list[str] | None = Field(
        default=None,
        max_length=20,
        description="Optional stage subset, using the same names as --stages.",
    )


# -- run state ----------------------------------------------------------------
class RunSummary(BaseModel):
    """One row of ``GET /api/runs`` — enough to render the history list."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    request: str
    project: str
    status: RunStatus
    stages: list[str]
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    ok: bool | None = None
    reason: str | None = None


class AgentOutput(BaseModel):
    """One stage's completed artifact from the task board."""

    model_config = ConfigDict(extra="forbid")

    agent: str
    status: str
    stage: str | None = None
    output: str
    files: list[str] = Field(default_factory=list)
    run_command: str | None = None
    verdict: str | None = None
    notes: list[str] = Field(default_factory=list)
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    tokens: dict[str, int] = Field(default_factory=dict)


class StageState(BaseModel):
    """One pipeline stage's state, including whether it is still to come."""

    model_config = ConfigDict(extra="forbid")

    stage: str
    agent: str
    status: str
    error: str | None = None
    output: str | None = None


class WrittenFile(BaseModel):
    """A file the pipeline wrote (or would write) in ``workspace/``."""

    model_config = ConfigDict(extra="forbid")

    path: str
    size: int
    status: str = "written"
    note: str | None = None


class RunStateResponse(BaseModel):
    """``GET /api/runs/{id}`` — the whole run, as the UI's detail view needs it.

    Assembled from the task board (the pipeline's own record) plus the pipeline
    result, never by re-running anything. Paths are workspace-relative, so the
    response cannot be used to read anything outside the sandbox (D15).
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    request: str
    project: str
    status: RunStatus
    options: dict[str, Any] = Field(default_factory=dict)
    stages: list[str] = Field(default_factory=list)
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    board: dict[str, Any] | None = Field(
        default=None, description="The shared task board, verbatim"
    )
    agents: list[AgentOutput] = Field(default_factory=list)
    stage_states: list[StageState] = Field(default_factory=list)
    files: list[WrittenFile] = Field(default_factory=list)
    tests: dict[str, Any] | None = Field(
        default=None, description="Latest test/execution result, if any"
    )
    budget: dict[str, int] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    last_seq: int = Field(-1, description="Highest event seq, for the SSE cursor")


class RunsListResponse(BaseModel):
    """``GET /api/runs`` returns this shape."""

    model_config = ConfigDict(extra="forbid")

    runs: list[RunSummary] = Field(default_factory=list)
    total: int = 0


class ProjectSummary(BaseModel):
    """One project folder under ``workspace/``."""

    model_config = ConfigDict(extra="forbid")

    project: str
    runs: int = 0
    last_run_at: str | None = None


class ProjectsListResponse(BaseModel):
    """``GET /api/projects`` returns this shape."""

    model_config = ConfigDict(extra="forbid")

    projects: list[ProjectSummary] = Field(default_factory=list)
    total: int = 0


class CancelResponse(BaseModel):
    """``POST /api/runs/{id}/cancel`` — best effort by design.

    ``cancelled`` is reported as soon as the request is accepted. The pipeline
    thread notices the flag at its next checkpoint and stops; if a generated
    command is running at that moment its whole process tree is killed (D15).
    ``note`` explains the case where there was nothing to cancel.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    cancelled: bool
    status: RunStatus
    note: str = ""


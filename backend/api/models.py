"""Pydantic request/response models for the HTTP API.

Conventions used throughout (matching ``backend/core/provider/schemas.py``):
Pydantic v2 models, ``snake_case`` field names that mirror the JSON, ``Field``
constraints for every user-supplied string/list, and ``model_config`` set
explicitly rather than relying on defaults.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.core.orchestrator.pipeline import APPROVAL_GATES, STAGE_SPECS

# -- run lifecycle ------------------------------------------------------------
#: Lifecycle states for a run created through the API. ``cancelling`` is the short
#: window between accepting a cancel and the pipeline actually stopping;
#: ``waiting_approval`` is a run paused at a plan/architecture/execution gate
#: (Part A step 3), holding the active slot until approved, rejected or cancelled.
RunStatus = Literal[
    "queued",
    "running",
    "cancelling",
    "waiting_approval",
    "succeeded",
    "failed",
    "cancelled",
]

#: Statuses from which no further transition happens.
TERMINAL_STATUSES: frozenset[str] = frozenset({"succeeded", "failed", "cancelled"})

#: Upper bound on file entries in a run-state response. A real run has tens; the
#: cap stops a runaway agent from turning one response into a directory listing.
MAX_FILES_IN_RESPONSE = 500


def utc_now() -> str:
    """UTC timestamp in the same shape the event bus uses."""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class HealthResponse(BaseModel):
    """``GET /api/health`` — enough for a launcher to know the server is up."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    version: str
    api_host: Literal["127.0.0.1"] = "127.0.0.1"
    #: The port the server actually bound. Not a guess: a launcher that asked
    #: for port 0 (ephemeral) needs this to point the desktop shell at us.
    api_port: int
    #: Echoed so a client can confirm the token works without a second endpoint.
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
        default=False, description="Report the file writes without making them"
    )
    no_apply: bool = Field(
        default=False, description="Do not write generated files into workspace/"
    )
    no_run_tests: bool = Field(
        default=False, description="Do not execute the tester's command"
    )
    #: Mirrors the CLI's ``--provider``/``--model``: one candidate for every
    #: agent in this run. Per-agent assignment is a *setting* (step 4), not a
    #: per-request field, so there is exactly one way to choose a model.
    provider: str | None = Field(
        default=None, min_length=1, max_length=40, description="Provider name override"
    )
    model: str | None = Field(
        default=None, min_length=1, max_length=120, description="Model id override"
    )
    stages: list[str] | None = Field(
        default=None,
        max_length=20,
        description="Optional stage subset, using the same names as --stages.",
    )
    #: Per-agent routing override: ``[["coder", "groq", "llama-3.3-70b"], …]``.
    #: Validated to exactly three non-empty strings so a malformed override is a
    #: 422 with a readable message rather than a 500 inside the worker.
    override: list[list[str]] | None = Field(default=None, max_length=20)
    #: Approval gates this run must pause at before continuing (D21): "plan"
    #: (after the planner), "architecture" (after the architect) and/or
    #: "execution" (before every command). Omitted/empty means no pauses.
    approval_gates: list[str] | None = Field(
        default=None,
        max_length=len(APPROVAL_GATES),
        description="Human approval gates: plan, architecture, execution.",
    )

    @field_validator("approval_gates")
    @classmethod
    def _check_approval_gates(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        for gate in value:
            if gate not in APPROVAL_GATES:
                raise ValueError(
                    f"unknown approval gate '{gate}'; known gates are: "
                    f"{', '.join(APPROVAL_GATES)}"
                )
        if len(set(value)) != len(value):
            raise ValueError("approval_gates must not repeat a gate")
        return value

    @field_validator("override")
    @classmethod
    def _check_override(cls, value: list[list[str]] | None) -> list[list[str]] | None:
        if value is None:
            return None
        for item in value:
            if len(item) != 3 or not all(part.strip() for part in item):
                raise ValueError(
                    "each override entry must be [agent, provider, model] with all "
                    "three parts non-empty"
                )
        return value

    @field_validator("stages")
    @classmethod
    def _check_stages(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        known = ", ".join(STAGE_SPECS)
        for stage in value:
            if stage not in STAGE_SPECS:
                raise ValueError(
                    f"unknown stage '{stage}'; known stages are: {known}"
                )
        return value


class RunOptions(BaseModel):
    """The resolved options of a run: what was asked for, not what was sent.

    ``stages`` is always concrete here (the registry's pipeline order when the
    request did not name a subset), so the UI never has to read YAML to know
    what a queued run will do.
    """

    model_config = ConfigDict(extra="forbid")

    stages: list[str]
    dry_run: bool = False
    apply_workspace: bool = True
    run_tests: bool = True
    provider: str | None = None
    model: str | None = None
    override: list[list[str]] | None = None
    #: resolved approval gates: always a concrete list, empty when none asked
    approval_gates: list[str] = Field(default_factory=list)

    def to_response(self) -> dict[str, Any]:
        """The three switches as the *request* named them.

        The API speaks the CLI's vocabulary: the user asked for ``--no-run-tests``,
        so the response reports ``no_run_tests``. Inverting back to
        ``run_tests=False`` here would make the UI re-derive the flag it sent.
        """
        return {
            "dry_run": self.dry_run,
            "no_apply": not self.apply_workspace,
            "no_run_tests": not self.run_tests,
        }

    @classmethod
    def from_request(
        cls, payload: CreateRunRequest, *, default_stages: list[str]
    ) -> RunOptions:
        """Resolve a request into concrete options, exactly like the CLI does."""
        return cls(
            stages=list(payload.stages or default_stages),
            dry_run=payload.dry_run,
            # --no-apply / --no-run-tests invert the request's negative flags.
            apply_workspace=not payload.no_apply,
            run_tests=not payload.no_run_tests,
            provider=payload.provider,
            model=payload.model,
            override=[list(item) for item in payload.override] if payload.override else None,
            approval_gates=list(payload.approval_gates or []),
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
    #: "provider/model" that actually answered this stage
    target: str = ""
    attempts: int = 0


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
    #: 1-based place in the queue when another run holds the slot, else None.
    #: Lets the UI say "waiting: 2nd" without reconstructing the whole queue.
    queue_position: int | None = None
    #: The current (or last) approval gate: gate name, what is being approved,
    #: the decision and its note. Non-None exactly while the run is waiting and
    #: for the finished run's last gate, so the UI can show why a run stopped.
    gate: dict[str, Any] | None = None


class RunsListResponse(BaseModel):
    """``GET /api/runs`` returns this shape."""

    model_config = ConfigDict(extra="forbid")

    runs: list[RunSummary] = Field(default_factory=list)
    total: int = 0
    #: The run currently executing, or None. Exactly one can be active (D20).
    active_run_id: str | None = None


class ProjectSummary(BaseModel):
    """One project folder under ``workspace/``.

    Only the folder name and a file count are exposed — never a filesystem path
    and never file contents, so the response cannot be used to read outside the
    sandbox (D15).

    The field is ``project``, not ``name``: it is the same slug the run record
    and the board carry, and the UI keys its project list off it directly.
    """

    model_config = ConfigDict(extra="forbid")

    project: str
    file_count: int = 0
    created_at: str | None = None


class ProjectsListResponse(BaseModel):
    """``GET /api/projects`` returns this shape, newest folder first."""

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


class ApprovalDecisionRequest(BaseModel):
    """Optional body of ``POST /api/runs/{id}/approve`` and ``/reject``.

    ``note`` is what the human wants recorded — mandatory in spirit for a
    rejection (it becomes the run's ``error``), optional for an approval.
    """

    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(
        default=None, max_length=2000, description="Why, recorded in the event log"
    )


class ApprovalResponse(BaseModel):
    """``POST /api/runs/{id}/approve`` / ``/reject`` — the decision, recorded.

    ``status`` is the run's status right after the decision was accepted; the
    worker thread may still be waking up, so it can briefly read
    ``waiting_approval`` — the UI keeps polling ``GET /api/runs/{id}`` for the
    terminal state, exactly as it does after a cancel.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    gate: str
    decision: Literal["approved", "rejected"]
    status: RunStatus
    note: str | None = None


class BusyResponse(BaseModel):
    """409 body when another run holds the active slot (D20)."""

    model_config = ConfigDict(extra="forbid")

    detail: str
    active_run_id: str



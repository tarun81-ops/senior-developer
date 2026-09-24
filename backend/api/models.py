"""Pydantic request/response models for the HTTP API.

Conventions used throughout (matching ``backend/core/provider/schemas.py``):
Pydantic v2 models, ``snake_case`` field names that mirror the JSON, ``Field``
constraints for every user-supplied string/list, and ``model_config`` set
explicitly rather than relying on defaults.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    """``GET /api/health`` — enough for a launcher to know the server is up."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    version: str
    api_host: Literal["127.0.0.1"] = "127.0.0.1"
    #: The port the server was told to bind. Not ``uvicorn``'s internal default —
    #: a launcher needs to know where to point the desktop shell.
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

"""Typed shapes for everything the provider layer exchanges.

Validating config with Pydantic means a typo in ``providers.yaml`` produces a
clear error message instead of a mysterious ``KeyError`` three layers deeper.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["system", "user", "assistant"]


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ChatMessage(_Base):
    role: Role
    content: str

    def as_payload(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}

    @staticmethod
    def system(content: str) -> ChatMessage:
        return ChatMessage(role="system", content=content)

    @staticmethod
    def user(content: str) -> ChatMessage:
        return ChatMessage(role="user", content=content)


class Usage(_Base):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_api(cls, raw: dict[str, Any] | None) -> Usage:
        raw = raw or {}
        prompt = int(raw.get("prompt_tokens") or 0)
        completion = int(raw.get("completion_tokens") or 0)
        total = int(raw.get("total_tokens") or (prompt + completion))
        return cls(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total)


class ProviderLimits(_Base):
    """Free-tier ceilings. ``0`` means "unknown: track usage but never block"."""

    requests_per_minute: int = 0
    requests_per_day: int = 0
    tokens_per_minute: int = 0
    tokens_per_day: int = 0


class ModelSpec(_Base):
    #: Filled in automatically from the key used in providers.yaml when omitted,
    #: so config stays DRY:  models: {gemini-3.8-flash: {context_window: 1000000}}
    id: str = ""
    context_window: int = 0
    max_output_tokens: int = 4096
    good_at: list[str] = Field(default_factory=list)


class MockBehaviour(_Base):
    """Controls the offline mock provider (demos + tests)."""

    reply: str = "(mock reply)"
    #: fail this many consecutive calls before starting to succeed
    fail_times: int = 0
    #: one of: rate_limit, auth, credits, server, network, model_not_found, bad_request
    fail_with: str = "rate_limit"
    #: simulated Retry-After header for the mock 429
    retry_after: float | None = None


class ProviderSpec(_Base):
    name: str
    label: str = ""
    kind: Literal["openai_compatible", "mock"] = "openai_compatible"
    base_url: str
    api_key_env: str = ""
    notes: str = ""
    extra_headers: dict[str, str] = Field(default_factory=dict)
    limits: ProviderLimits = Field(default_factory=ProviderLimits)
    models: dict[str, ModelSpec] = Field(default_factory=dict)
    mock: MockBehaviour | None = None

    @property
    def requires_key(self) -> bool:
        return self.kind != "mock" and bool(self.api_key_env)

    def model(self, model_id: str) -> ModelSpec:
        spec = self.models.get(model_id)
        if spec is None:
            return ModelSpec(id=model_id)
        if not spec.id:
            return spec.model_copy(update={"id": model_id})
        return spec

    def quota_key(self, model_id: str) -> str:
        """Usage is tracked per provider *and* model.

        Gemini applies its daily limit per model, per project, so tracking only
        the provider would under-count and lead to surprise 429s.
        """
        return f"{self.name}:{model_id}"


class Completion(_Base):
    """A successful model call plus the story of how we got there."""

    text: str
    provider: str
    model: str
    usage: Usage = Field(default_factory=Usage)
    latency_ms: int = 0
    attempts: list[str] = Field(default_factory=list)
    failed_over: bool = False
    finish_reason: str | None = None

    @property
    def target(self) -> str:
        return f"{self.provider}/{self.model}"

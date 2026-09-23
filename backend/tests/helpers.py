"""Shared test helpers.

Everything here builds providers/registries in memory, so the whole test suite
runs offline, with no API keys, and never touches the real config files.
"""

from __future__ import annotations

from pathlib import Path

from backend.core.config import PACKAGE_ROOT
from backend.core.events import EventBus, JsonlWriter
from backend.core.orchestrator.budgets import BudgetTracker
from backend.core.provider.client import OpenAICompatClient
from backend.core.provider.ratelimit import QuotaLedger
from backend.core.provider.registry import BudgetConfig, Registry
from backend.core.provider.router import ProviderRouter, RouterOptions
from backend.core.provider.schemas import ProviderSpec

MISSING_KEY_ENV = "UNIT_TEST_KEY_THAT_IS_NOT_SET"


def mock_provider(
    name: str,
    *,
    fail_times: int = 0,
    fail_with: str = "rate_limit",
    retry_after: float | None = None,
    reply: str = "mock reply",
    limits: dict | None = None,
) -> ProviderSpec:
    """A provider that behaves exactly as configured, with no network."""
    return ProviderSpec.model_validate(
        {
            "name": name,
            "kind": "mock",
            "base_url": "mock://local",
            "limits": limits or {},
            "models": {"m": {"id": "m", "max_output_tokens": 128}},
            "mock": {
                "reply": reply,
                "fail_times": fail_times,
                "fail_with": fail_with,
                "retry_after": retry_after,
            },
        }
    )


def keyed_provider(name: str, *, limits: dict | None = None) -> ProviderSpec:
    """A provider that needs an API key which is deliberately never set."""
    return ProviderSpec.model_validate(
        {
            "name": name,
            "kind": "openai_compatible",
            "base_url": "https://example.invalid/v1",
            "api_key_env": MISSING_KEY_ENV,
            "limits": limits or {},
            "models": {"m": {"id": "m"}},
        }
    )


def build_registry(providers: list[ProviderSpec], *, routing: list[tuple[str, str]] | None = None,
                   limits: dict | None = None) -> Registry:
    providers_raw = {
        spec.name: spec.model_dump() for spec in providers
    }
    chain = routing or [(spec.name, "m") for spec in providers]
    agents_raw = {
        "tester_agent": {
            "prompt_file": "backend/core/agents/prompts/code.md",
            "temperature": 0.0,
            "max_output_tokens": 64,
            "routing": [{"provider": p, "model": m} for p, m in chain],
        }
    }
    return Registry.from_dict(
        providers_raw={"providers": providers_raw},
        agents_raw={"agents": agents_raw},
        limits_raw=limits or {},
        root=PACKAGE_ROOT,
    )


def build_router(
    tmp_path: Path,
    registry: Registry,
    *,
    bus: EventBus | None = None,
    ledger: QuotaLedger | None = None,
    budget: BudgetTracker | None = None,
    backoff_scale: float = 0.0,
) -> tuple[ProviderRouter, EventBus]:
    bus = bus or EventBus(
        run_id="test-run", jsonl=JsonlWriter(tmp_path / "events.jsonl"), echo=False
    )
    ledger = ledger or QuotaLedger(tmp_path / "quota.json", save=False)
    router = ProviderRouter(
        registry,
        ledger=ledger,
        bus=bus,
        budget=budget,
        client=OpenAICompatClient(timeout_seconds=5),
        options=RouterOptions(backoff_scale=backoff_scale, sleep=lambda _seconds: None),
    )
    return router, bus


def budget_for(tmp_path: Path, **overrides) -> BudgetTracker:
    config = BudgetConfig(**overrides)
    return BudgetTracker(config, run_id="test-run", path=tmp_path / "budget.json")


def event_kinds(records: list) -> list[str]:
    return [record["kind"] for record in records]

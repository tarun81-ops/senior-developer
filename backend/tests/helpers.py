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


def build_pipeline_runtime(
    tmp_path: Path,
    *,
    reviewer_reply: str = '{"verdict": "approve"}',
    default_reply: str = "mock reply",
    max_fix_iterations: int = 1,
    stages: list[str] | None = None,
    limits: dict | None = None,
    replies: dict[str, str] | None = None,
    apply_workspace: bool = False,
    run_tests: bool = False,
):
    """A Runtime whose seven specialist agents all run on offline mocks.

    ``reviewer_reply`` controls the verdict JSON (or plain text) the reviewer
    stage returns, which is how the fix-loop behaviour is exercised for free.
    ``replies`` overrides the reply for individual agents (e.g. a coder that
    emits a file manifest), and ``apply_workspace``/``run_tests`` switch on the
    Phase 3 behaviour with everything rooted in ``tmp_path``.
    """
    from backend.core.config import Settings
    from backend.core.runtime import Runtime

    stem = PACKAGE_ROOT / "backend" / "core" / "agents" / "prompts" / "code.md"
    prompt = str(stem)  # absolute: the temp root has no prompt files of its own
    replies = dict(replies or {})

    providers: dict[str, dict] = {}
    agents: dict[str, dict] = {}
    for name in ("planner", "architect", "coder", "tester", "devops", "docs", "reviewer"):
        reply = replies.get(name)
        if reply is None:
            reply = reviewer_reply if name == "reviewer" else default_reply
        provider = mock_provider(f"mock_{name}", reply=reply)
        providers[provider.name] = provider.model_dump()
        agents[name] = {
            "prompt_file": prompt,
            "temperature": 0.0,
            "max_output_tokens": 256,
            "routing": [{"provider": provider.name, "model": "m"}],
        }

    pipeline_raw = {
        "stages": stages
        or ["planner", "architect", "coder", "tester", "reviewer", "devops", "docs"],
        "max_fix_iterations": max_fix_iterations,
        "apply_workspace": apply_workspace,
        "run_tests": run_tests,
    }
    registry = Registry.from_dict(
        providers_raw={"providers": providers},
        agents_raw={"agents": agents, "pipeline": pipeline_raw},
        limits_raw=limits or {},
        root=PACKAGE_ROOT,
    )

    run_id = "pipeline-test"
    # a throwaway project root: workspace/ and data/ stay inside tmp_path
    root = tmp_path / "project-root"
    bus = EventBus(run_id=run_id, jsonl=JsonlWriter(tmp_path / "events.jsonl"), echo=False)
    ledger = QuotaLedger(tmp_path / "quota.json", save=False)
    budget = BudgetTracker.load(
        registry.limits.budget, run_id=run_id, path=tmp_path / "budget.json"
    )
    router, bus = build_router(
        tmp_path, registry, bus=bus, ledger=ledger, budget=budget, backoff_scale=0.0
    )
    return Runtime(
        settings=Settings.from_root(root),
        registry=registry,
        ledger=ledger,
        bus=bus,
        budget=budget,
        router=router,
    )


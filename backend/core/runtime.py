"""Composition root: wire the pieces together in one place.

Both the CLI (Phase 1) and the FastAPI app (Phase 4) build a :class:`Runtime`
and then use it. Keeping construction in one place means there is exactly one
answer to "how is a router configured?".
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from backend.core.agents import Agent
from backend.core.config import PACKAGE_ROOT, Settings, get_settings, load_env
from backend.core.events import EventBus, JsonlWriter
from backend.core.orchestrator.budgets import BudgetTracker
from backend.core.provider.client import OpenAICompatClient
from backend.core.provider.ratelimit import QuotaLedger
from backend.core.provider.registry import AgentConfig, Registry
from backend.core.provider.router import ProviderRouter, RouterOptions
from backend.core.research import Researcher


def new_run_id() -> str:
    """Sortable, unique-ish id: ``20260924-181530-4f2a``."""
    import os

    return f"{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(2).hex()}"


@dataclass
class Runtime:
    settings: Settings
    registry: Registry
    ledger: QuotaLedger
    bus: EventBus
    budget: BudgetTracker
    router: ProviderRouter
    #: web search for every agent (D42); None without FIRECRAWL_API_KEY
    researcher: Researcher | None = None

    @property
    def run_id(self) -> str:
        return self.bus.run_id

    @property
    def events_path(self) -> Path:
        return self.settings.runs_dir / self.run_id / "events.jsonl"

    @classmethod
    def create(
        cls,
        *,
        root: Path | str | None = None,
        run_id: str | None = None,
        echo: bool = True,
        backoff_scale: float = 1.0,
        persist: bool = True,
        transport: httpx.BaseTransport | None = None,
    ) -> Runtime:
        settings = get_settings(root)
        load_env(settings.root)
        registry = Registry.load(settings)

        run_id = run_id or new_run_id()
        run_dir = settings.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        bus = EventBus(
            run_id=run_id,
            jsonl=JsonlWriter(run_dir / "events.jsonl") if persist else None,
            echo=echo,
        )
        ledger = QuotaLedger(settings.data_dir / "quota.json", save=persist)
        budget = BudgetTracker.load(
            registry.limits.budget, run_id=run_id, path=run_dir / "budget.json"
        )
        client = OpenAICompatClient(
            timeout_seconds=registry.limits.request.timeout_seconds, transport=transport
        )
        router = ProviderRouter(
            registry,
            ledger=ledger,
            bus=bus,
            budget=budget,
            client=client,
            options=RouterOptions(backoff_scale=backoff_scale, sleep=time.sleep),
        )
        return cls(
            settings=settings,
            registry=registry,
            ledger=ledger,
            bus=bus,
            budget=budget,
            router=router,
            researcher=Researcher.from_env(),
        )

    # -- agents --------------------------------------------------------------
    def agent_config(self, name: str) -> AgentConfig:
        return self.registry.agent(name)

    def agent(self, name: str) -> Agent:
        # Relative prompt paths resolve against the package, not the data root:
        # prompt files ship with the code, and an installed app's data root
        # (%APPDATA%) has none (D33). In a repo checkout the two are the same.
        return Agent(
            self.registry.agent(name),
            router=self.router,
            bus=self.bus,
            root=PACKAGE_ROOT,
            researcher=self.researcher,
        )

    def close(self) -> None:
        self.router.close()
        if self.researcher:
            self.researcher.close()

"""Pytest fixtures shared by the test modules."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.events import EventBus, JsonlWriter
from backend.core.provider.ratelimit import QuotaLedger
from backend.core.provider.registry import CooldownConfig, RetryConfig


@pytest.fixture
def events_file(tmp_path: Path) -> Path:
    return tmp_path / "events.jsonl"


@pytest.fixture
def bus(events_file: Path) -> EventBus:
    """A bus that records to a temp file and stays quiet on the terminal."""
    return EventBus(run_id="test-run", jsonl=JsonlWriter(events_file), echo=False)


@pytest.fixture
def ledger(tmp_path: Path) -> QuotaLedger:
    """In-memory quota ledger (nothing written to the real data/ folder)."""
    return QuotaLedger(tmp_path / "quota.json", save=False)


@pytest.fixture
def retry_config() -> RetryConfig:
    return RetryConfig(
        max_attempts_per_provider=3,
        base_delay_seconds=1.5,
        max_delay_seconds=10.0,
        jitter=False,
    )


@pytest.fixture
def cooldown_config() -> CooldownConfig:
    return CooldownConfig()

"""Tests for quota tracking, cooldowns and backoff maths.

These tests are the safety net for the part of the system that protects your
free-tier quota: get it wrong and runs die halfway through with 429s.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone

import pytest

from backend.core.provider.ratelimit import (
    QuotaLedger,
    backoff_delay,
    cooldown_seconds,
    pacific_day,
)
from backend.core.provider.registry import CooldownConfig, RetryConfig
from backend.tests.helpers import mock_provider


# --------------------------------------------------------------------------- #
# backoff
# --------------------------------------------------------------------------- #
def test_backoff_is_exponential(retry_config: RetryConfig) -> None:
    delays = [backoff_delay(attempt, retry_config) for attempt in (1, 2, 3, 4)]
    assert delays == [1.5, 3.0, 6.0, 10.0]  # 4th capped at max_delay_seconds


def test_retry_after_header_beats_exponential_backoff(retry_config: RetryConfig) -> None:
    assert backoff_delay(3, retry_config, retry_after=7.0) == 7.0


def test_retry_after_is_scaled_for_demos(retry_config: RetryConfig) -> None:
    assert backoff_delay(1, retry_config, retry_after=20.0, scale=0.0) == 0.0


def test_jitter_stays_within_50_percent() -> None:
    config = RetryConfig(base_delay_seconds=4.0, max_delay_seconds=100.0, jitter=True)
    rng = random.Random(1234)
    for attempt in (1, 2, 3):
        base = 4.0 * (2 ** (attempt - 1))
        for _ in range(50):
            delay = backoff_delay(attempt, config, rng=rng)
            assert 0.5 * base <= delay <= 1.5 * base


# --------------------------------------------------------------------------- #
# the Pacific day boundary (Gemini resets its daily quota there)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("utc_hour", "utc_minute", "expected_day"),
    [
        (6, 59, "2026-09-23"),  # 23:59 Pacific on the 23rd
        (7, 0, "2026-09-24"),   # 00:00 Pacific on the 24th
        (12, 0, "2026-09-24"),
    ],
)
def test_pacific_day_boundary(utc_hour: int, utc_minute: int, expected_day: str) -> None:
    moment = datetime(2026, 9, 24, utc_hour, utc_minute, tzinfo=timezone.utc)
    assert pacific_day(moment) == expected_day


# --------------------------------------------------------------------------- #
# ledger
# --------------------------------------------------------------------------- #
def test_ledger_counts_requests_and_tokens(ledger: QuotaLedger) -> None:
    ledger.record_attempt("p:m", tokens=100)
    ledger.record_attempt("p:m")
    usage = ledger.usage("p:m")
    assert usage.requests == 2
    assert usage.tokens == 100
    assert ledger.requests_last_minute("p:m") == 2


def test_ledger_blocks_at_daily_request_cap(ledger: QuotaLedger) -> None:
    spec = mock_provider("p", limits={"requests_per_day": 2})
    for _ in range(2):
        ledger.record_attempt(spec.quota_key("m"))
    allowed, reason = ledger.check(spec, "m")
    assert allowed is False
    assert "daily request limit" in reason


def test_ledger_blocks_at_per_minute_cap(ledger: QuotaLedger) -> None:
    spec = mock_provider("p", limits={"requests_per_minute": 1})
    ledger.record_attempt(spec.quota_key("m"))
    allowed, reason = ledger.check(spec, "m")
    assert allowed is False
    assert "per-minute" in reason


def test_zero_limits_mean_unknown_never_block(ledger: QuotaLedger) -> None:
    spec = mock_provider("p")  # all limits default to 0
    for _ in range(500):
        ledger.record_attempt(spec.quota_key("m"))
    allowed, _ = ledger.check(spec, "m")
    assert allowed is True


def test_ledger_resets_when_the_pacific_day_changes(ledger: QuotaLedger) -> None:
    spec = mock_provider("p", limits={"requests_per_day": 1})
    ledger.record_attempt(spec.quota_key("m"))
    assert ledger.check(spec, "m")[0] is False

    # pretend the counters were recorded on an older day
    ledger._usage[spec.quota_key("m")].day = "1999-01-01"
    assert ledger.usage(spec.quota_key("m")).requests == 0
    assert ledger.check(spec, "m")[0] is True


def test_ledger_survives_a_restart(tmp_path) -> None:
    path = tmp_path / "quota.json"
    first = QuotaLedger(path)
    first.record_attempt("p:m", tokens=42)
    second = QuotaLedger(path)
    assert second.usage("p:m").requests == 1
    assert second.usage("p:m").tokens == 42


# --------------------------------------------------------------------------- #
# cooldowns
# --------------------------------------------------------------------------- #
def test_cooldown_blocks_then_expires(tmp_path) -> None:
    clock = {"now": 1000.0}
    ledger = QuotaLedger(tmp_path / "q.json", clock=lambda: clock["now"], save=False)
    spec = mock_provider("p")

    ledger.set_cooldown("p", reason="rate_limit", seconds=60)
    allowed, reason = ledger.check(spec, "m")
    assert allowed is False
    assert "cooling down" in reason and "rate_limit" in reason

    clock["now"] += 61
    assert ledger.cooldown_remaining("p") == 0.0
    assert ledger.check(spec, "m")[0] is True


def test_zero_second_cooldown_is_ignored(tmp_path) -> None:
    ledger = QuotaLedger(tmp_path / "q.json", save=False)
    ledger.set_cooldown("p", reason="model_not_found", seconds=0)
    assert ledger.cooldown_remaining("p") == 0.0


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("rate_limit", 60), ("auth", 900), ("credits", 3600), ("server", 30), ("network", 20)],
)
def test_cooldown_lengths_by_error_kind(kind: str, expected: int) -> None:
    assert cooldown_seconds(kind, CooldownConfig()) == expected


def test_config_mistakes_do_not_punish_the_provider() -> None:
    # a bad model id is OUR mistake, so no cooldown is applied
    assert cooldown_seconds("model_not_found", CooldownConfig()) == 0

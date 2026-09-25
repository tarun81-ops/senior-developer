"""Quota tracking, cooldowns and backoff maths.

Free tiers have three separate ceilings (requests per minute, requests per day,
tokens per day) and they are enforced by the provider, not by us. If we do not
track them ourselves we discover them as random 429s halfway through a build.
So this module keeps a small ledger on disk:

    data/quota.json
    {
      "providers": {"gemini:gemini-3.8-flash": {"day": "2026-09-24",
                                                "requests": 12,
                                                "tokens": 34567,
                                                "recent_requests": [1758...] }},
      "cooldowns": {"mock_flaky": {"until": 1758..., "reason": "rate_limit"}}
    }

It also implements the circuit breaker ("cooldown"): after certain failures we
stop using a provider for a while instead of hammering it.
"""

from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.core.provider.registry import CooldownConfig, RetryConfig
from backend.core.provider.schemas import ProviderSpec

try:  # pragma: no cover - tzdata is required on Windows, bundled on Linux
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

# Gemini's daily quota resets at midnight *Pacific*, not local time.
PACIFIC_TZ = "America/Los_Angeles"
MINUTE_WINDOW = 60.0


def pacific_day(moment: datetime | None = None) -> str:
    """Return the current ``YYYY-MM-DD`` in the Pacific timezone."""
    moment = moment or datetime.now(UTC)
    if ZoneInfo is None:  # pragma: no cover - extremely defensive
        return moment.astimezone(UTC).strftime("%Y-%m-%d")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(ZoneInfo(PACIFIC_TZ)).strftime("%Y-%m-%d")


def backoff_delay(
    attempt: int,
    retry: RetryConfig,
    *,
    retry_after: float | None = None,
    rng: random.Random | None = None,
    scale: float = 1.0,
) -> float:
    """Seconds to wait before attempt number ``attempt + 1``.

    Rules, in order of priority:
      1. an explicit ``Retry-After`` from the provider always wins
      2. otherwise ``base * 2**(attempt-1)``, capped by ``max_delay_seconds``
      3. optional jitter (+/-50%) so parallel agents do not retry in lockstep

    ``scale`` exists for tests and demos: scale=0 makes retries instant.
    """
    if retry_after is not None and retry_after > 0:
        return max(0.0, retry_after * scale)
    base = max(0.0, retry.base_delay_seconds)
    delay = min(base * (2 ** max(0, attempt - 1)), max(0.0, retry.max_delay_seconds))
    if retry.jitter and delay > 0:
        jitter_rng = rng or random.Random()
        delay *= 0.5 + jitter_rng.random()  # 0.5x .. 1.5x
    return max(0.0, delay * scale)


@dataclass
class ProviderUsage:
    """Usage counters for one ``provider:model`` pair."""

    day: str = ""
    requests: int = 0
    tokens: int = 0
    recent_requests: list[float] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "requests": self.requests,
            "tokens": self.tokens,
            # only the last two minutes are ever needed for the RPM window
            "recent_requests": self.recent_requests[-120:],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> ProviderUsage:
        raw = raw or {}
        return cls(
            day=str(raw.get("day") or ""),
            requests=int(raw.get("requests") or 0),
            tokens=int(raw.get("tokens") or 0),
            recent_requests=[float(x) for x in (raw.get("recent_requests") or [])],
        )


class QuotaLedger:
    """Persistent per-provider usage counters plus cooldown (circuit breaker) state."""

    def __init__(self, path: Path | str, *, clock: Any = time.time, save: bool = True) -> None:
        self.path = Path(path)
        self.save_enabled = save
        self._clock = clock
        # RLock because the small helpers below call each other while locked
        self._lock = threading.RLock()
        self._usage: dict[str, ProviderUsage] = {}
        self._cooldowns: dict[str, dict[str, Any]] = {}
        self._load()

    # -- persistence --------------------------------------------------------
    def _load(self) -> None:
        """Load whatever previous runs recorded.

        ``save=False`` is read-only mode (used by ``doctor`` and
        ``providers --no-persist``): it must still *show* the real quota and
        cooldown state, it just must not write anything back.
        """
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self._usage = {
            key: ProviderUsage.from_dict(value)
            for key, value in (raw.get("providers") or {}).items()
        }
        self._cooldowns = dict(raw.get("cooldowns") or {})

    def _flush(self) -> None:
        if not self.save_enabled:
            return
        payload = {
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "providers": {k: v.to_dict() for k, v in self._usage.items()},
            "cooldowns": self._cooldowns,
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        # write-then-rename so a crash never leaves a half-written ledger
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # -- usage --------------------------------------------------------------
    def usage(self, key: str, *, now: float | None = None) -> ProviderUsage:
        """Current counters for a provider, rolling over at midnight Pacific."""
        now = self._clock() if now is None else now
        today = pacific_day()
        with self._lock:
            usage = self._usage.get(key)
            if usage is None:
                usage = ProviderUsage(day=today)
                self._usage[key] = usage
            if usage.day != today:
                usage.day = today
                usage.requests = 0
                usage.tokens = 0
                usage.recent_requests = []
            usage.recent_requests = [t for t in usage.recent_requests if now - t < MINUTE_WINDOW]
            return usage

    def record_attempt(self, key: str, *, tokens: int = 0) -> None:
        """Count a request. Failed requests count too: they used a request slot."""
        now = self._clock()
        with self._lock:
            usage = self.usage(key, now=now)
            usage.requests += 1
            usage.tokens += max(0, tokens)
            usage.recent_requests.append(now)
            self._flush()

    def record_tokens(self, key: str, tokens: int) -> None:
        with self._lock:
            self.usage(key).tokens += max(0, tokens)
            self._flush()

    def requests_last_minute(self, key: str) -> int:
        return len(self.usage(key).recent_requests)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {key: usage.to_dict() for key, usage in self._usage.items()}

    # -- cooldowns (circuit breaker) ----------------------------------------
    # Cooldowns are keyed by PROVIDER, not by model: a bad key, an empty wallet
    # or a global outage affects the whole account, so switching models on the
    # same provider would not help.
    def set_cooldown(self, provider: str, *, reason: str, seconds: float) -> float:
        if seconds <= 0:
            return 0.0
        until = self._clock() + float(seconds)
        with self._lock:
            self._cooldowns[provider] = {
                "until": until,
                "reason": reason,
                "seconds": float(seconds),
            }
            self._flush()
        return until

    def clear_cooldown(self, provider: str) -> None:
        with self._lock:
            if self._cooldowns.pop(provider, None) is not None:
                self._flush()

    def cooldown_remaining(self, provider: str, *, now: float | None = None) -> float:
        now = self._clock() if now is None else now
        record = self._cooldowns.get(provider)
        if not record:
            return 0.0
        remaining = float(record.get("until") or 0) - now
        if remaining <= 0:
            with self._lock:
                self._cooldowns.pop(provider, None)
                self._flush()
            return 0.0
        return remaining

    def cooldown_reason(self, provider: str) -> str:
        record = self._cooldowns.get(provider) or {}
        return str(record.get("reason") or "")

    # -- admission control ---------------------------------------------------
    def check(
        self,
        spec: ProviderSpec,
        model_id: str,
        *,
        now: float | None = None,
    ) -> tuple[bool, str]:
        """Can we use this provider/model right now?

        Returns ``(allowed, reason)``. A limit of ``0`` in the config means
        "unknown", in which case we track usage but never block.
        """
        now = self._clock() if now is None else now
        remaining = self.cooldown_remaining(spec.name, now=now)
        if remaining > 0:
            reason = self.cooldown_reason(spec.name) or "failure"
            return False, f"cooling down {remaining:.0f}s more (last failure: {reason})"

        usage = self.usage(spec.quota_key(model_id), now=now)
        limits = spec.limits
        if limits.requests_per_day and usage.requests >= limits.requests_per_day:
            return False, (
                f"daily request limit reached ({usage.requests}/{limits.requests_per_day})"
            )
        if limits.tokens_per_day and usage.tokens >= limits.tokens_per_day:
            return False, (
                f"daily token limit reached ({usage.tokens}/{limits.tokens_per_day})"
            )
        per_minute = len(usage.recent_requests)
        if limits.requests_per_minute and per_minute >= limits.requests_per_minute:
            return False, (
                f"per-minute request limit reached ({per_minute}/{limits.requests_per_minute})"
            )
        return True, "ok"


#: Which cooldown length applies to which class of error.
COOLDOWN_FIELDS: dict[str, str] = {
    "rate_limit": "on_rate_limit_seconds",
    "auth": "on_auth_error_seconds",
    "credits": "on_credits_seconds",
    "server": "on_server_error_seconds",
    "network": "on_network_error_seconds",
    "empty": "on_empty_seconds",
}


def cooldown_seconds(kind: str, cooldowns: CooldownConfig) -> int:
    """Look up the configured cooldown length for an error kind.

    Unknown kinds (e.g. ``model_not_found``) return 0: that is a config mistake
    on OUR side, so it should move to the next provider without punishing the
    provider itself.
    """
    field_name = COOLDOWN_FIELDS.get(kind)
    return int(getattr(cooldowns, field_name, 0)) if field_name else 0



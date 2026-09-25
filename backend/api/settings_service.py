"""Settings for the API: model choices and write-only API keys (D22).

Every function loads a fresh :class:`Registry`, the same call a new run makes.
That makes the response exactly what the *next* run will use. A run already in
progress holds its own registry, and nothing here touches it.
"""

from __future__ import annotations

import os
import threading

from backend.api.models import (
    AgentSettings,
    ProviderSettings,
    RouteEntry,
    SettingsResponse,
)
from backend.core.config import Settings, load_env
from backend.core.local_settings import (
    API_KEY_PATTERN,
    LocalOverrides,
    write_env_values,
    write_overrides,
)
from backend.core.provider.registry import Registry

#: Deploy credentials (Phase 5, D41). Write-only like provider keys (D22):
#: saved to .env, reported only as set/missing. RENDER_OWNER_ID is not a
#: secret but is handled the same way, which keeps one simple rule.
DEPLOY_KEYS = ("GITHUB_TOKEN", "RENDER_API_KEY", "RENDER_OWNER_ID")

#: Route handlers run in a threadpool. Two PUTs must not share a temp file.
_write_lock = threading.Lock()


def snapshot(settings: Settings) -> SettingsResponse:
    """Effective settings: tracked config plus the saved overrides.

    A stale or corrupt overrides file reads as no overrides (the defaults the
    next run will really use), so the UI can still load and fix it.
    """
    registry, overrides, warning = Registry.load_effective(settings)
    return SettingsResponse(
        agents=[
            AgentSettings(
                agent=name,
                routing=[RouteEntry(provider=c.provider, model=c.model) for c in agent.routing],
                override=overrides.agents.get(name),
            )
            for name, agent in registry.agents.items()
        ],
        providers=[
            ProviderSettings(
                provider=name,
                label=spec.label or name,
                kind=spec.kind,
                models=sorted(spec.models),
                requires_key=spec.requires_key,
                api_key_env=spec.api_key_env,
            )
            for name, spec in registry.providers.items()
        ],
        provider_order=list(overrides.provider_order),
        keys={
            **{
                spec.api_key_env: "set" if registry.api_key_for(spec) else "missing"
                for spec in _keyed(registry)
            },
            **{name: "set" if os.environ.get(name) else "missing" for name in DEPLOY_KEYS},
        },
        warnings=[warning] if warning else [],
    )


def update_overrides(settings: Settings, overrides: LocalOverrides) -> SettingsResponse:
    """Validate against the configured names, then persist. Raises ``ConfigError``."""
    Registry.load(settings, overrides=overrides)  # the whole validation
    with _write_lock:
        write_overrides(settings.local_settings_path, overrides)
    return snapshot(settings)


def update_keys(settings: Settings, keys: dict[str, str]) -> SettingsResponse:
    """Write keys to ``.env`` only. Raises ``ValueError`` naming variables, never values."""
    # Expected names come from the tracked config alone, so a broken local
    # overrides file cannot block fixing a key.
    tracked = Registry.load(settings, overrides=LocalOverrides())
    expected = sorted([spec.api_key_env for spec in _keyed(tracked)] + list(DEPLOY_KEYS))
    unknown = sorted(set(keys) - set(expected))
    if unknown:
        raise ValueError(
            f"Unknown key variable(s): {', '.join(unknown)}. Expected: {', '.join(expected)}"
        )
    bad = sorted(name for name, value in keys.items() if not API_KEY_PATTERN.match(value))
    if bad:
        raise ValueError(f"Value for {', '.join(bad)} is empty or has characters a key never has")
    with _write_lock:
        write_env_values(settings.env_path, keys)
        load_env(settings.root)  # .env wins (D12): the next run reads the new key
    return snapshot(settings)


def _keyed(registry: Registry) -> list:
    return [s for s in registry.providers.values() if s.requires_key and s.api_key_env]

"""API key lookup. Keys are never written to the repository.

Lookup order:
  1. process environment (including anything loaded from ``.env``)
  2. Windows Credential Manager, via ``keyring`` -- this is what the desktop UI
     will use in Phase 4 so keys never sit in a text file at all

``keyring`` is imported lazily and is optional, so Phase 1 works with a plain
``.env`` file.
"""

from __future__ import annotations

import os

SERVICE_NAME = "senior-developer-agents"


def get_api_key(env_var: str, *, provider: str = "") -> str | None:
    """Return the key for ``env_var`` or ``None`` when it is not configured."""
    if not env_var:
        return None

    value = os.environ.get(env_var)
    if value and value.strip():
        return value.strip()

    try:  # pragma: no cover - depends on optional dependency
        import keyring
    except ImportError:
        return None

    try:  # pragma: no cover - depends on OS credential store
        stored = keyring.get_password(SERVICE_NAME, env_var)
    except Exception:
        return None
    return stored.strip() if stored and stored.strip() else None


def has_api_key(env_var: str, *, provider: str = "") -> bool:
    return get_api_key(env_var, provider=provider) is not None


def set_api_key(env_var: str, value: str) -> bool:
    """Store a key in the OS credential store. Returns False if unavailable."""
    try:  # pragma: no cover - optional dependency
        import keyring
    except ImportError:
        return False
    try:  # pragma: no cover - depends on OS credential store
        keyring.set_password(SERVICE_NAME, env_var, value)
    except Exception:
        return False
    return True


def mask(value: str | None) -> str:
    """Show a key's shape without leaking it: ``AIza...9f2c``."""
    if not value:
        return "(not set)"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def describe_sources() -> dict[str, str]:
    """Report where the running process reads keys from (never the values)."""
    sources = {"environment": "yes", "keyring": "unavailable"}
    try:  # pragma: no cover - optional dependency
        import keyring

        backend = keyring.get_keyring()
        sources["keyring"] = getattr(backend, "name", backend.__class__.__name__)
    except Exception:
        pass
    return sources

"""Local settings overrides and ``.env`` key writes (D18, D22).

Two git-ignored files, two jobs:

* ``data/settings.local.yaml`` holds the user's model choices: a pinned
  first-choice model per agent and a global provider order. It is layered over
  the tracked ``config/agents.yaml`` every time a :class:`Registry` loads, so the
  tracked YAML is never edited and every *new* run sees the latest choice.
* ``.env`` receives API keys. Nothing here ever reads a key back out.

Both are written atomically (temp file, then ``replace``), the same pattern as
the task board and the quota ledger.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from backend.core.errors import ConfigError

#: What a real provider key looks like. Deliberately excludes whitespace,
#: quotes, ``#`` and ``$`` so a value can neither break a ``.env`` line nor be
#: read back differently by python-dotenv (comments, ``${VAR}`` interpolation).
API_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._\-:/+=~]{1,512}$")


class AgentPin(BaseModel):
    """The model an agent should try first; its configured chain stays behind it."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(..., min_length=1, max_length=40)
    model: str = Field(..., min_length=1, max_length=120)


class LocalOverrides(BaseModel):
    """Everything ``data/settings.local.yaml`` may contain."""

    model_config = ConfigDict(extra="forbid")

    agents: dict[str, AgentPin] = Field(default_factory=dict, max_length=50)
    provider_order: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("provider_order")
    @classmethod
    def _no_duplicates(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("provider_order lists a provider more than once")
        return value


def read_overrides(path: Path) -> LocalOverrides:
    """The saved overrides, or none when the file does not exist yet."""
    if not path.exists():
        return LocalOverrides()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return LocalOverrides.model_validate(raw)
    except (yaml.YAMLError, ValidationError) as exc:
        raise ConfigError(f"{path} is not valid; fix or delete it: {exc}") from exc


def write_overrides(path: Path, overrides: LocalOverrides) -> None:
    """Replace the overrides file atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "# Written by the settings API (D22). Git-ignored; delete to reset.\n"
    text += yaml.safe_dump(overrides.model_dump(), sort_keys=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def apply_overrides(
    agents_raw: dict[str, Any], overrides: LocalOverrides, *, providers: set[str]
) -> dict[str, Any]:
    """Return a copy of ``agents.yaml`` data with the overrides layered on.

    * ``provider_order`` reorders only the chain slots held by the providers it
      names; any other provider keeps its position. That way the ``demo``
      agent's deliberately failing first provider stays first, and so does the
      offline mock at the end of every chain.
    * An agent pin moves that provider/model to the front, and the rest of the
      configured chain stays behind it as the fallback.

    Unknown agents and providers raise :class:`ConfigError`. Unknown models
    are caught by the registry's normal routing check.
    """
    unknown = [name for name in overrides.provider_order if name not in providers]
    if unknown:
        raise ConfigError(f"provider_order names unknown provider(s): {', '.join(unknown)}")
    merged = copy.deepcopy(agents_raw)
    agents = merged.get("agents") or {}
    missing = [name for name in overrides.agents if name not in agents]
    if missing:
        raise ConfigError(f"Override for unknown agent(s): {', '.join(missing)}")

    rank = {name: i for i, name in enumerate(overrides.provider_order)}
    for name, raw in agents.items():
        routing = list((raw or {}).get("routing") or [])
        slots = [i for i, c in enumerate(routing) if c.get("provider") in rank]
        ordered = sorted((routing[i] for i in slots), key=lambda c: rank[c["provider"]])
        for i, candidate in zip(slots, ordered, strict=True):
            routing[i] = candidate
        pin = overrides.agents.get(name)
        if pin is not None:
            first = pin.model_dump()
            routing = [first] + [c for c in routing if c != first]
        agents[name] = {**(raw or {}), "routing": routing}
    return merged


def write_env_values(env_path: Path, values: dict[str, str]) -> None:
    """Set ``NAME=value`` lines in ``.env`` atomically, keeping every other line.

    Callers validate names and values first. This function never returns or
    logs a value.
    """
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    pending = dict(values)
    out: list[str] = []
    for line in lines:
        name, sep, _ = line.partition("=")
        name = name.strip()
        if sep and name in pending:
            out.append(f"{name}={pending.pop(name)}")
        else:
            out.append(line)
    out.extend(f"{name}={value}" for name, value in pending.items())
    tmp = env_path.with_name(env_path.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    tmp.replace(env_path)

"""Settings and filesystem paths.

Why this file exists: every path in the system is derived from the repository
root, so the sandbox folder (``workspace/``) and the state folder (``data/``)
can never be confused with a random folder on disk.

``PACKAGE_ROOT`` is computed from this file's location:

    backend/core/config.py  ->  parents[0] = core
                                parents[1] = backend
                                parents[2] = repository root
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

try:  # python-dotenv is optional at runtime so the core never hard-depends on it
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - only hit if deps are not installed
    load_dotenv = None


PACKAGE_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    """Absolute paths for one project checkout."""

    root: Path
    config_dir: Path
    data_dir: Path
    workspace_dir: Path
    runs_dir: Path
    db_path: Path

    @classmethod
    def from_root(cls, root: Path | str) -> Settings:
        root = Path(root).resolve()
        data_dir = root / "data"
        return cls(
            root=root,
            config_dir=root / "config",
            data_dir=data_dir,
            workspace_dir=root / "workspace",
            runs_dir=data_dir / "runs",
            db_path=data_dir / "app.db",
        )

    def ensure_dirs(self) -> None:
        """Create the folders the runtime needs (safe to call repeatedly)."""
        for path in (self.data_dir, self.workspace_dir, self.runs_dir):
            path.mkdir(parents=True, exist_ok=True)

    @property
    def config_files(self) -> dict[str, Path]:
        return {
            "providers": self.config_dir / "providers.yaml",
            "agents": self.config_dir / "agents.yaml",
            "limits": self.config_dir / "limits.yaml",
        }


def load_env(root: Path | str | None = None, *, override: bool = False) -> Path | None:
    """Load ``.env`` into the process environment if it exists.

    Returns the path that was loaded, or ``None``. Existing environment
    variables win by default, so a key exported in the shell beats a stale
    ``.env`` file.
    """
    if load_dotenv is None:
        return None
    env_path = Path(root).resolve() / ".env" if root else PACKAGE_ROOT / ".env"
    if not env_path.exists():
        return None
    load_dotenv(env_path, override=override)
    return env_path


def get_settings(root: Path | str | None = None) -> Settings:
    """Build a :class:`Settings` object and make sure its folders exist."""
    settings = Settings.from_root(root or PACKAGE_ROOT)
    settings.ensure_dirs()
    return settings


def read_yaml(path: Path) -> dict[str, Any]:
    """Read a YAML file that must contain a mapping."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config file must contain a YAML mapping: {path}")
    return data


def env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean environment flag such as ``AGENTS_DEBUG=1``."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}

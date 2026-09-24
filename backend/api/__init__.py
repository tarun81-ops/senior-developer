"""FastAPI backend for the desktop UI (Phase 4, Part A).

Thin HTTP layer on top of ``backend.core``: it never re-implements the
pipeline, the provider router or the sandbox — those stay in ``backend.core``
and are used exactly as the CLI uses them.
"""

from __future__ import annotations

__all__ = ["create_app"]


def create_app(*args, **kwargs):  # noqa: ANN002, ANN003 - re-exported lazily
    """Build the FastAPI application (see :mod:`backend.api.app`)."""
    from backend.api.app import create_app as _create_app

    return _create_app(*args, **kwargs)

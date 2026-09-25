"""In-memory GitHub + Render for the desktop's development smoke (D41).

``python -m backend.api`` uses these instead of the real services only when
``SDA_FAKE_DEPLOY=1`` is set. This module lives in backend/tests, which the
installer does not ship (desktop/scripts/bundle-backend.mjs), so an installed
app cannot be switched to fakes: the launcher refuses to start instead.
"""

from __future__ import annotations

import os

import httpx

from backend.core.deploy.github import GitHubClient
from backend.core.deploy.render import RenderClient
from backend.tests.test_deploy_github import FakeGitHub
from backend.tests.test_deploy_render import FakeRender

#: one fake of each per process, so redeploys find what earlier deploys made
GITHUB = FakeGitHub()
RENDER = FakeRender()


def clients(target: str) -> tuple[GitHubClient, RenderClient | None]:
    """The deploy_clients seam of create_app, backed by the fakes.

    The real key variables are still required, so the "set your keys first"
    path works exactly as it does against the real services.
    """
    github = GitHubClient(
        os.environ.get("GITHUB_TOKEN", ""),
        transport=httpx.MockTransport(GITHUB),
        sleep=lambda _s: None,
    )
    if target != "render":
        return github, None
    render = RenderClient(
        os.environ.get("RENDER_API_KEY", ""),
        owner_id=os.environ.get("RENDER_OWNER_ID", ""),
        transport=httpx.MockTransport(RENDER),
        sleep=lambda _s: None,
    )
    return github, render

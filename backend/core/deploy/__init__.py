"""Deploying generated projects (Phase 5; D11 amended, D35, D36)."""

from backend.core.deploy.detect import Detection, detect, repo_name
from backend.core.deploy.eligibility import deploy_refusal

__all__ = ["Detection", "deploy_refusal", "detect", "repo_name"]

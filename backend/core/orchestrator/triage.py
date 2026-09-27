"""Task-size triage: pick the pipeline's stage list before any agent runs.

A heuristic on the raw goal text, not an LLM call - triage has to be free and
instant, and "is this a calculator or a distributed system" does not need a
model call to answer.
"""

from __future__ import annotations

#: goal mentions any of these -> definitely not a fast-lane task
_LARGE_HINTS = (
    "microservice", "database", "auth", "authentication", "multi-user",
    "distributed", "queue", "websocket", "payment", "kubernetes", "docker",
    "multiple services", "api gateway", "concurrent users", "real-time",
    "scalable", "cloud", "deployment pipeline",
)
#: a goal this short, with no large-hint, is a fast-lane task
_SMALL_WORD_LIMIT = 12
_LARGE_WORD_LIMIT = 40

#: the fast lane: Coder -> auto-checks -> done. No planner/architect/review/devops/docs.
FAST_LANE_STAGES = ["coder"]


def classify(goal: str) -> str:
    """``"small"``, ``"medium"`` or ``"large"``, from the goal text alone."""
    text = (goal or "").strip().lower()
    words = text.split()
    if any(hint in text for hint in _LARGE_HINTS) or len(words) > _LARGE_WORD_LIMIT:
        return "large"
    if len(words) <= _SMALL_WORD_LIMIT:
        return "small"
    return "medium"

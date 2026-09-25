"""Which runs may be deployed at all (D35).

Only a run that **succeeded** and whose tests **actually ran and passed**.
Every refusal says why, so the UI can show it instead of a disabled button
with no explanation. This is checked on the server, never only in the UI.
"""

from __future__ import annotations

from typing import Any


def deploy_refusal(run: dict[str, Any]) -> str | None:
    """``None`` if the run may be deployed, else the reason it may not.

    ``run`` is the run state as ``GET /api/runs/{id}`` returns it.
    """
    status = run.get("status")
    if status != "succeeded":
        reason = (run.get("result") or {}).get("reason")
        if status == "failed" and reason == "interrupted":
            return "the run was interrupted; only runs that succeeded can be deployed"
        return f"the run {status or 'has no status'}; only runs that succeeded can be deployed"
    options = run.get("options") or {}
    if options.get("dry_run"):
        return "it was a dry run, so nothing was written to deploy"
    if options.get("no_apply"):
        return "its files were never written to the workspace"
    if options.get("no_run_tests"):
        return "its tests were skipped; only runs whose tests ran and passed can be deployed"
    tests = run.get("tests")
    if not tests:
        return "no tests ran for this run; only runs whose tests ran and passed can be deployed"
    if tests.get("timed_out"):
        return "its tests timed out"
    if not tests.get("ok") or tests.get("exit_code") not in (0, None):
        return "its last test run failed"
    return None

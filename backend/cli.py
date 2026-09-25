"""Command line entry point.

    python -m backend.cli doctor              # is my setup ready?
    python -m backend.cli providers            # keys + today's quota
    python -m backend.cli models --provider groq --live
    python -m backend.cli ask "explain git rebase in 3 lines"
    python -m backend.cli ask "..." --provider groq --model openai/gpt-oss-120b
    python -m backend.cli build "a pomodoro timer CLI in Python"   # full specialist pipeline
    python -m backend.cli build "..." --stages planner,architect   # part of it
    python -m backend.cli demo                 # offline retry + failover demo
    python -m backend.cli events -n 25         # tail the last run's log

Everything prints the same structured events that the desktop UI will show in
Phase 4, because both read from the same event bus.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from backend import __version__
from backend.core.agents import Agent
from backend.core.config import get_settings, load_env
from backend.core.errors import AgentSystemError, BudgetExceeded, ConfigError
from backend.core.events import find_run_events, iter_records
from backend.core.orchestrator import Pipeline, TaskBoard
from backend.core.provider.ratelimit import pacific_day
from backend.core.runtime import Runtime
from backend.core.workspace import (
    CommandNotAllowed,
    CommandRunner,
    Workspace,
    detect_test_command,
    test_command_for,
)

EXIT_OK = 0
EXIT_PROBLEM = 1
EXIT_RUNTIME_ERROR = 2
EXIT_BUDGET = 3
#: the reviewer still says changes_requested after the fix loop: human decision
EXIT_REVIEW = 4
#: the workspace test run is still failing after the fix loop: human decision
EXIT_TESTS = 5


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _table(headers: list[str], rows: list[list[Any]], *, pad: int = 2) -> str:
    """Minimal fixed-width table (no third-party dependency needed)."""
    columns = len(headers)
    widths = [len(str(h)) for h in headers]
    for row in rows:
        for index in range(columns):
            cell = str(row[index]) if index < len(row) else ""
            widths[index] = max(widths[index], len(cell))
    lines = ["  ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    lines.append("  ".join("-" * widths[i] for i in range(columns)))
    for row in rows:
        cells = [str(row[i]) if i < len(row) else "" for i in range(columns)]
        lines.append("  ".join(cells[i].ljust(widths[i]) for i in range(columns)).rstrip())
    return "\n".join(lines) + ("\n" if pad else "")


def _ok(message: str) -> str:
    return f"[ OK ]  {message}"


def _warn(message: str) -> str:
    return f"[WARN]  {message}"


def _fail(message: str) -> str:
    return f"[FAIL]  {message}"


def _inject_mock_failure(
    runtime: Runtime, provider: str, fail_times: int, fail_with: str
) -> None:
    """Make a mock provider misbehave on purpose (used by --mock-fail-times)."""
    spec = runtime.registry.provider(provider)
    if spec.mock is None:
        raise ConfigError(f"Provider '{provider}' is not a mock provider")
    updated = spec.mock.model_copy(
        update={"fail_times": fail_times, "fail_with": fail_with}
    )
    runtime.registry.providers[provider] = spec.model_copy(update={"mock": updated})


def _single_candidate(runtime: Runtime, provider: str, model: str):
    """Build a one-entry routing chain from --provider/--model."""
    if not provider:
        return None
    spec, model_spec = runtime.registry.resolve(provider, model or "")
    if not model:
        if not spec.models:
            raise ConfigError(f"Provider '{provider}' has no models configured")
        fallback = "mock-echo" if "mock-echo" in spec.models else sorted(spec.models)[0]
        model_spec = spec.model(fallback)
    return [(spec, model_spec)]


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_doctor(args: argparse.Namespace) -> int:
    settings = get_settings()
    load_env(settings.root)
    problems = 0

    print(f"senior-developer-agents v{__version__}  (doctor)\n")
    print(_ok(f"Python {sys.version.split()[0]} ({sys.executable})"))
    if sys.version_info < (3, 11):  # noqa: UP036 - doctor reports it, not the import
        print(_fail("Python 3.11 or newer is required"))
        problems += 1
    elif sys.version_info >= (3, 14):
        print(
            _warn(
                "Python 3.14 is very new; if a package fails to install, use Python 3.12 "
                "or 3.13 for the virtual environment"
            )
        )

    for path in settings.config_files.values():
        if path.exists():
            print(_ok(f"config/{path.name} found"))
        else:
            print(_fail(f"config/{path.name} is missing"))
            problems += 1

    for folder in (settings.data_dir, settings.workspace_dir, settings.runs_dir):
        try:
            folder.mkdir(parents=True, exist_ok=True)
            probe = folder / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            print(_ok(f"writable: {folder}"))
        except OSError as exc:
            print(_fail(f"cannot write to {folder}: {exc}"))
            problems += 1

    print()
    try:
        # Short waits: the doctor is a smoke test, not a stress test.
        runtime = Runtime.create(echo=False, persist=False, backoff_scale=0.1)
    except AgentSystemError as exc:
        print(_fail(f"could not load configuration: {exc}"))
        return EXIT_PROBLEM

    try:
        for row in runtime.registry.provider_status():
            if not row["requires_key"]:
                print(_ok(f"{row['provider']}: offline mock, no key needed"))
            elif row["key_present"]:
                print(_ok(f"{row['provider']}: key found in {row['api_key_env']}"))
            else:
                print(
                    _warn(
                        f"{row['provider']}: no key yet -> set {row['api_key_env']} in .env "
                        "(the router will skip this provider)"
                    )
                )

        # An active cooldown silently removes a provider from every chain, so
        # the doctor has to say so out loud (and tell you how to undo it).
        for name in runtime.registry.providers:
            remaining = runtime.ledger.cooldown_remaining(name)
            if remaining > 0:
                reason = runtime.ledger.cooldown_reason(name) or "failure"
                print(
                    _warn(
                        f"{name} is cooling down for {remaining:.0f}s more "
                        f"(last failure: {reason}) -> after fixing the cause run: "
                        "python -m backend.cli providers --clear-cooldowns"
                    )
                )

        print()
        try:
            result = runtime.agent("demo").run("doctor connectivity check")
            print(_ok(f"offline route works end to end: answered by {result.target}"))
        except AgentSystemError as exc:
            print(_fail(f"offline route failed: {exc}"))
            problems += 1
    finally:
        runtime.close()

    print()
    if problems:
        print(f"{problems} problem(s) found.")
        return EXIT_PROBLEM
    print('All good. Next: add a key to .env, then run  python -m backend.cli ask "hi"')
    return EXIT_OK


def cmd_providers(args: argparse.Namespace) -> int:
    runtime = Runtime.create(echo=False, persist=not args.no_persist)
    try:
        if args.clear_cooldowns:
            cleared = [
                name
                for name in runtime.registry.providers
                if runtime.ledger.cooldown_remaining(name) > 0
            ]
            for name in cleared:
                runtime.ledger.clear_cooldown(name)
            print(
                f"Cleared cooldown for: {', '.join(sorted(cleared))}\n"
                if cleared
                else "No active cooldowns to clear.\n"
            )

        by_provider: dict[str, list[str]] = {}
        for agent_name, agent_cfg in runtime.registry.agents.items():
            for candidate in agent_cfg.routing:
                by_provider.setdefault(candidate.provider, []).append(agent_name)

        rows = []
        for row in runtime.registry.provider_status():
            key_source = "(offline)" if not row["requires_key"] else row["api_key_env"]
            if row["key_present"]:
                key_state = "set"
            else:
                key_state = "missing" if row["requires_key"] else "-"
            rows.append(
                [
                    row["provider"],
                    key_source,
                    key_state,
                    len(row["models"]),
                    ", ".join(sorted(set(by_provider.get(row["provider"], [])))) or "-",
                ]
            )
        print("Providers\n")
        print(_table(["name", "key from", "key", "models", "used by"], rows))

        print(f"Quota used today ({pacific_day()} Pacific -- Gemini resets at this boundary)\n")
        quota_rows = []
        for spec in runtime.registry.providers.values():
            for model_id in spec.models:
                usage = runtime.ledger.usage(spec.quota_key(model_id))
                if usage.requests:
                    quota_rows.append(
                        [
                            spec.name,
                            model_id,
                            usage.requests,
                            spec.limits.requests_per_day or "-",
                            usage.tokens,
                        ]
                    )
        if quota_rows:
            print(_table(["provider", "model", "requests", "daily cap", "tokens"], quota_rows))
        else:
            print("(no requests recorded yet)\n")

        cooldowns = []
        for name in runtime.registry.providers:
            remaining = runtime.ledger.cooldown_remaining(name)
            if remaining > 0:
                cooldowns.append(
                    [name, f"{remaining:.0f}s", runtime.ledger.cooldown_reason(name)]
                )
        if cooldowns:
            print("Currently on cooldown (circuit breaker)\n")
            print(_table(["provider", "remaining", "reason"], cooldowns))
        else:
            print("No provider is on cooldown.\n")
    finally:
        runtime.close()
    return EXIT_OK


def cmd_models(args: argparse.Namespace) -> int:
    runtime = Runtime.create(echo=False, persist=False)
    try:
        if args.provider:
            specs = [runtime.registry.provider(args.provider)]
        else:
            specs = list(runtime.registry.providers.values())

        for spec in specs:
            print(f"\n{spec.name}  ({spec.label or 'no label'})")
            if args.live:
                try:
                    key = runtime.registry.api_key_for(spec)
                    live = runtime.router.client.list_models(spec, api_key=key)
                    shown = [m for m in live if m.endswith(":free")] if args.free_only else live
                    for model_id in shown:
                        print(f"  {model_id}")
                    if not shown:
                        print("  (provider returned no models)")
                except AgentSystemError as exc:
                    print(f"  ! could not list models: {exc}")
                continue
            for model_id, model_spec in sorted(spec.models.items()):
                roles = ", ".join(model_spec.good_at) or "-"
                max_out = model_spec.max_output_tokens
                print(f"  {model_id:<48} max_out={max_out:<6} good at: {roles}")
        print("\nTip: add --live to ask the provider itself (needs a key).")
    finally:
        runtime.close()
    return EXIT_OK


def cmd_build(args: argparse.Namespace) -> int:
    """Run the specialist pipeline: plan -> design -> code -> test -> review..."""
    runtime = Runtime.create(
        echo=not (args.quiet or args.json),
        backoff_scale=0.1 if args.fast else 1.0,
    )
    try:
        stages = (
            [s.strip() for s in args.stages.split(",") if s.strip()]
            if args.stages
            else None
        )
        override = _single_candidate(runtime, args.provider, args.model)
        pipeline = Pipeline(
            runtime,
            stages=stages,
            override=override,
            project=args.project,
            apply_workspace=False if args.no_apply else None,
            run_tests=False if args.no_run_tests else None,
            dry_run=args.dry_run,
        )
        try:
            result = pipeline.run(args.goal)
        except BudgetExceeded as exc:
            print(f"\nBUDGET STOP: {exc}", file=sys.stderr)
            print("A human has to decide what happens next (that is by design).", file=sys.stderr)
            return EXIT_BUDGET

        if args.json:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            board = TaskBoard.load(result.board_path)
            rows = []
            for record in board.rows():
                detail = "; ".join(record["notes"]) or record["error"]
                rows.append(
                    [
                        record["stage"],
                        record["status"],
                        record["target"] or "-",
                        record["tokens"],
                        detail[:60],
                    ]
                )
            print("\n" + _table(["stage", "status", "target", "tokens", "notes"], rows))
            print(f"verdict    : {result.verdict or '(not reviewed)'}")
            counts = result.files or {}
            if counts:
                suffix = " (dry run: nothing written)" if args.dry_run else ""
                print(
                    f"workspace  : {result.workspace}  "
                    f"({counts.get('written', 0)} new, {counts.get('overwritten', 0)} updated, "
                    f"{counts.get('unchanged', 0)} unchanged, "
                    f"{counts.get('rejected', 0)} rejected){suffix}"
                )
            tests = result.tests or {}
            if tests:
                state = "ok" if tests.get("ok") else "FAILED"
                print(f"tests      : {tests.get('command')} -> {state}")
            else:
                print("tests      : not run")
            if result.reason == "review":
                print(
                    "review     : STILL REQUESTED CHANGES after the fix loop - "
                    "the human has to decide (board has the issues)."
                )
            if result.reason == "tests":
                print(
                    "tests      : STILL FAILING after the fix loop - the human has to "
                    "decide (the board holds the output)."
                )
            print(f"run budget : {runtime.budget.summary()}")
            print(f"board      : {result.board_path}")
            print(f"events     : {result.events_file}")

        if result.ok:
            return EXIT_OK
        # a human decision is needed; which one is in `reason`
        return EXIT_TESTS if result.reason == "tests" else EXIT_REVIEW
    except ConfigError:
        raise  # a wrong config is a setup problem, not a runtime failure
    finally:
        runtime.close()


def _board_for(settings, project: str | None):
    """Newest run whose board matches ``project`` (or the newest board at all)."""
    boards = sorted(
        settings.runs_dir.glob("*/board.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in boards:
        try:
            board = TaskBoard.load(path)
        except (OSError, ValueError):
            continue
        if project is None or board.project == project:
            return board, path
    return None, None


def cmd_run(args: argparse.Namespace) -> int:
    """Re-run a generated project's tests. No model calls, no quota spent."""
    runtime = Runtime.create(echo=False)
    try:
        workspace = Workspace(runtime.settings.workspace_dir)
        project = args.project
        board, board_path = _board_for(runtime.settings, project)
        if not project and board is not None:
            project = board.project or None
        if not project:
            candidates = [path.name for path in workspace.projects()]
            if len(candidates) == 1:
                project = candidates[0]
            elif candidates:
                print(
                    f"Several projects exist: {', '.join(candidates)}. "
                    "Pass --project NAME.",
                    file=sys.stderr,
                )
                return EXIT_PROBLEM
            else:
                print(
                    'No generated project yet. Run: python -m backend.cli build "todo app"',
                    file=sys.stderr,
                )
                return EXIT_PROBLEM

        project_dir = workspace.project_dir(project)
        command = args.command
        source = "cli"
        if not command:
            if board is not None:
                command, source = test_command_for(board, project_dir)
            if not command:
                command = detect_test_command(project_dir) or ""
                source = "detected"
        if not command:
            print(
                f"No test command: '{project}' has no tester run_command and nothing "
                "could be detected. Pass --command.",
                file=sys.stderr,
            )
            return EXIT_PROBLEM

        runner = CommandRunner(runtime.registry.limits.execution)
        runtime.bus.emit(
            "exec.start", f"{command}  (from {source})", project=project, command=command
        )
        try:
            result = runner.run(command, cwd=project_dir)
        except CommandNotAllowed as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            runtime.bus.emit("exec.skipped", str(exc), command=command)
            return EXIT_PROBLEM
        if board is not None:
            board.add_execution(result)
        runtime.bus.emit(
            "exec.end",
            f"{command}: {result.summary()}",
            project=project,
            command=command,
            ok=result.ok,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
        )

        if args.json:
            print(
                json.dumps(
                    {
                        "project": project,
                        "cwd": str(project_dir),
                        "board": str(board_path) if board_path else "",
                        **result.to_dict(),
                    },
                    indent=2,
                )
            )
        else:
            print(f"project : {project}")
            print(f"cwd     : {project_dir}")
            print(f"command : {command}  (from {source})")
            if result.stdout.strip():
                print("\n--- stdout ---\n" + result.stdout.strip())
            if result.stderr.strip():
                print("\n--- stderr ---\n" + result.stderr.strip())
            print(f"\n{result.summary()}")
            if board_path:
                print(f"board   : {board_path}")

        return EXIT_OK if result.ok else EXIT_TESTS
    except ConfigError:
        raise
    finally:
        runtime.close()


def cmd_ask(args: argparse.Namespace) -> int:
    runtime = Runtime.create(
        echo=not (args.quiet or args.json),
        backoff_scale=0.1 if args.fast else 1.0,
    )
    try:
        if args.mock_fail_times is not None:
            _inject_mock_failure(
                runtime, args.provider or "mock_flaky", args.mock_fail_times, args.mock_fail_with
            )

        override = _single_candidate(runtime, args.provider, args.model)
        agent: Agent = runtime.agent(args.agent)
        started = time.perf_counter()
        try:
            result = agent.run(
                args.prompt,
                override=override,
                temperature=args.temperature,
                max_output_tokens=args.max_tokens,
            )
        except BudgetExceeded as exc:
            print(f"\nBUDGET STOP: {exc}", file=sys.stderr)
            print("A human has to decide what happens next (that is by design).", file=sys.stderr)
            return EXIT_BUDGET
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        completion = result.completion

        if args.json:
            print(
                json.dumps(
                    {
                        "agent": result.agent,
                        "text": result.text,
                        "parsed": result.parsed,
                        "provider": completion.provider,
                        "model": completion.model,
                        "usage": completion.usage.model_dump(),
                        "latency_ms": completion.latency_ms,
                        "attempts": completion.attempts,
                        "failed_over": completion.failed_over,
                        "run_id": runtime.run_id,
                        "events_file": str(runtime.events_path),
                    },
                    indent=2,
                )
            )
            return EXIT_OK

        print("\n" + "=" * 72)
        print(result.text)
        print("=" * 72)
        print(
            "\n".join(
                [
                    f"provider   : {completion.provider}",
                    f"model      : {completion.model}",
                    f"tokens     : {completion.usage.total_tokens}"
                    f" (in {completion.usage.prompt_tokens}"
                    f" / out {completion.usage.completion_tokens})",
                    f"latency    : {completion.latency_ms} ms (wall clock {elapsed_ms} ms)",
                    f"attempts   : {' | '.join(completion.attempts) or '-'}",
                    f"failover   : {'yes' if completion.failed_over else 'no'}",
                    f"run budget : {runtime.budget.summary()}",
                    f"events     : {runtime.events_path}",
                ]
            )
        )
        if result.parsed is not None:
            print("parsed JSON: yes (usable by later phases)")
        return EXIT_OK
    except ConfigError:
        raise  # a wrong config is a setup problem, not a runtime failure
    except AgentSystemError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR
    finally:
        runtime.close()


def cmd_demo(args: argparse.Namespace) -> int:
    """Show retry, backoff, cooldown and failover with zero cost."""
    scale = 0.2 if not args.real_delays else 1.0
    print(
        "\nOffline demonstration. The chain for the 'demo' agent is:\n"
        "  1. mock_flaky  -> always answers HTTP 429 (simulated rate limit)\n"
        "  2. mock         -> always answers successfully\n"
        f"\nWaits are scaled x{scale} so this finishes quickly; the real numbers live in "
        "config/limits.yaml.\nWatch for: provider.rate_limited -> provider.retry -> "
        "provider.failover -> llm.response.\n"
    )
    runtime = Runtime.create(echo=not args.quiet, backoff_scale=scale)
    try:
        result = runtime.agent("demo").run("demonstrate failover")
        report = runtime.router.last_report
        print()
        print(report.describe() if report else "(no report)")
        print(f"\nfinal answer from : {result.target}")
        print(f"text              : {result.text}")
        print(f"flaky provider    : cooldown {runtime.ledger.cooldown_remaining('mock_flaky'):.0f}s"
              f" (reason: {runtime.ledger.cooldown_reason('mock_flaky') or '-'})")
        print(f"events written to : {runtime.events_path}")
        print("\nThe same code path handles real 429s from Gemini/Groq/OpenRouter.")
        return EXIT_OK
    except ConfigError:
        raise
    except AgentSystemError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR
    finally:
        runtime.close()


def cmd_events(args: argparse.Namespace) -> int:
    settings = get_settings()
    path = find_run_events(settings.runs_dir, args.run_id)
    if path is None:
        print("No run logs found yet. Run something first, e.g.  python -m backend.cli ask \"hi\"")
        return EXIT_OK

    records = list(iter_records(path))
    if args.kind:
        records = [r for r in records if r.get("kind") == args.kind]
    records = records[-args.limit :]

    print(f"{path}\n")
    for record in records:
        stamp = str(record.get("ts", ""))[11:23]
        kind = str(record.get("kind", ""))
        provider = str(record.get("provider") or "")
        model = str(record.get("model") or "")
        target = f"{provider}/{model}" if provider else ""
        who = f"[{record.get('agent')}] " if record.get("agent") else ""
        print(f"{stamp}  {kind:<19} {target:<34} {who}{record.get('message', '')}")
    print(f"\n{len(records)} event(s) shown. Filter with --kind llm.response")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# argument parsing
# --------------------------------------------------------------------------- #
EXAMPLES = """examples:
  python -m backend.cli doctor
  python -m backend.cli providers
  python -m backend.cli models --provider groq --live
  python -m backend.cli ask "explain git rebase in 3 bullet points"
  python -m backend.cli ask "list 3 pytest commands" --provider mock
  python -m backend.cli ask "reply with JSON" --json
  python -m backend.cli demo
  python -m backend.cli events --kind llm.response -n 10
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backend.cli",
        description="Multi-agent software engineering system - Phase 1 (provider layer).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EXAMPLES,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check Python, config, folders, keys, offline route")
    doctor.set_defaults(func=cmd_doctor)

    providers = sub.add_parser("providers", help="providers, keys, quota used today, cooldowns")
    providers.add_argument(
        "--no-persist",
        action="store_true",
        help="read the quota ledger without writing to it",
    )
    providers.add_argument(
        "--clear-cooldowns",
        action="store_true",
        help="forget active circuit-breaker cooldowns (do this after fixing a key)",
    )
    providers.set_defaults(func=cmd_providers)

    models = sub.add_parser("models", help="models from config, or --live from the provider")
    models.add_argument("--provider", help="only this provider")
    models.add_argument("--live", action="store_true", help="ask the provider's /models endpoint")
    models.add_argument("--free-only", action="store_true", help="only ids ending in :free")
    models.set_defaults(func=cmd_models)

    ask = sub.add_parser("ask", help="send one prompt to one agent")
    ask.add_argument("prompt", help="what you want (quote it in PowerShell)")
    ask.add_argument("--agent", default="code", help="agent name from config/agents.yaml")
    ask.add_argument("--provider", help="force a single provider (disables failover)")
    ask.add_argument("--model", help="model id for --provider")
    ask.add_argument("--temperature", type=float, default=None)
    ask.add_argument("--max-tokens", type=int, default=None)
    ask.add_argument("--json", action="store_true", help="machine-readable output only")
    ask.add_argument("--quiet", action="store_true", help="hide the live event log")
    ask.add_argument("--fast", action="store_true", help="shorten retry waits 10x (for demos)")
    ask.add_argument(
        "--mock-fail-times",
        type=int,
        default=None,
        help="make the selected mock provider fail this many calls first",
    )
    ask.add_argument(
        "--mock-fail-with",
        default="rate_limit",
        choices=["rate_limit", "auth", "credits", "server", "network", "model_not_found"],
    )
    ask.set_defaults(func=cmd_ask)

    build = sub.add_parser(
        "build",
        help="run the specialist pipeline: plan, design, code, test, review, devops, docs",
    )
    build.add_argument("goal", help="plain-English description of what to build")
    build.add_argument(
        "--stages",
        help="comma-separated subset of stages, e.g. planner,architect",
    )
    build.add_argument("--provider", help="force a single provider for every stage")
    build.add_argument("--model", help="model id for --provider")
    build.add_argument("--project", help="workspace folder name (default: slug of the goal)")
    build.add_argument(
        "--no-apply", action="store_true", help="do not write files into workspace/"
    )
    build.add_argument(
        "--no-run-tests", action="store_true", help="do not execute the tester's command"
    )
    build.add_argument(
        "--dry-run", action="store_true", help="report what would be written, write nothing"
    )
    build.add_argument("--json", action="store_true", help="machine-readable output only")
    build.add_argument("--quiet", action="store_true", help="hide the live event log")
    build.add_argument("--fast", action="store_true", help="shorten retry waits 10x (for demos)")
    build.set_defaults(func=cmd_build)

    run = sub.add_parser(
        "run", help="re-run a generated project's tests (no model calls, no quota)"
    )
    run.add_argument("--project", help="workspace project (default: the newest run's project)")
    run.add_argument("--command", help="run this instead of the tester's run_command")
    run.add_argument("--json", action="store_true", help="machine-readable output only")
    run.add_argument("--quiet", action="store_true", help="reserved: run prints its own summary")
    run.set_defaults(func=cmd_run)

    demo = sub.add_parser("demo", help="offline retry + backoff + failover demonstration")
    demo.add_argument("--quiet", action="store_true", help="hide the live event log")
    demo.add_argument(
        "--real-delays",
        action="store_true",
        help="use the full waits from config/limits.yaml instead of shortened demo waits",
    )
    demo.set_defaults(func=cmd_demo)

    events = sub.add_parser("events", help="tail the event log of a run")
    events.add_argument("--run-id", help="defaults to the most recent run")
    events.add_argument("--kind", help="filter, e.g. llm.response")
    events.add_argument("-n", "--limit", type=int, default=40, help="how many events to show")
    events.set_defaults(func=cmd_events)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\nStopped by user.", file=sys.stderr)
        return 130
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return EXIT_PROBLEM
    except AgentSystemError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR


if __name__ == "__main__":
    raise SystemExit(main())






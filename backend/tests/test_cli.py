"""End-to-end CLI tests.

These run the real command line entry point against a temporary project root
(the real ``config/`` plus the real prompt files), so they verify the wiring
between CLI, config, runtime, router and event log - still with no network and
no API keys, because 'mock' is the last provider in the chain.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from backend import cli
from backend.core import config as config_module
from backend.core.config import PACKAGE_ROOT


@pytest.fixture
def fake_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway project root that looks like the real one."""
    shutil.copytree(PACKAGE_ROOT / "config", tmp_path / "config")
    prompts_src = PACKAGE_ROOT / "backend" / "core" / "agents" / "prompts"
    prompts_dst = tmp_path / "backend" / "core" / "agents" / "prompts"
    prompts_dst.mkdir(parents=True)
    for prompt in prompts_src.glob("*.md"):
        shutil.copy(prompt, prompts_dst / prompt.name)

    monkeypatch.setattr(config_module, "PACKAGE_ROOT", tmp_path)
    return tmp_path


def test_ask_with_the_mock_provider(capsys: pytest.CaptureFixture[str], fake_root: Path) -> None:
    exit_code = cli.main(["ask", "hello there", "--provider", "mock", "--quiet"])
    output = capsys.readouterr().out

    assert exit_code == cli.EXIT_OK
    assert "MOCK OK" in output
    assert "provider   : mock" in output
    # the run log must exist for the UI to replay later
    runs = list((fake_root / "data" / "runs").glob("*/events.jsonl"))
    assert len(runs) == 1


def test_ask_json_output_is_machine_readable(
    capsys: pytest.CaptureFixture[str], fake_root: Path
) -> None:
    import json

    exit_code = cli.main(["ask", "hi", "--provider", "mock", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == cli.EXIT_OK
    assert payload["provider"] == "mock"
    assert payload["model"] == "mock-echo"
    assert "text" in payload


def test_forced_broken_provider_reports_all_providers_failed(
    capsys: pytest.CaptureFixture[str], fake_root: Path
) -> None:
    # mock_flaky always returns 429, and --provider makes it the only candidate,
    # so there is nothing left to fail over to.
    exit_code = cli.main(["ask", "hi", "--provider", "mock_flaky", "--fast", "--quiet"])
    captured = capsys.readouterr()

    assert exit_code == cli.EXIT_RUNTIME_ERROR
    assert "all providers failed" in captured.err.lower()
    assert "rate_limit" in captured.err


def test_demo_shows_the_whole_failover_story(
    capsys: pytest.CaptureFixture[str], fake_root: Path
) -> None:
    exit_code = cli.main(["demo", "--quiet"])
    output = capsys.readouterr().out

    assert exit_code == cli.EXIT_OK
    assert "final answer from : mock/mock-echo" in output
    assert "mock_flaky" in output
    events = (fake_root / "data" / "runs").glob("*/events.jsonl")
    log = next(events).read_text(encoding="utf-8")
    assert "provider.failover" in log
    assert "provider.retry" in log


def test_providers_command_lists_configured_providers(
    capsys: pytest.CaptureFixture[str], fake_root: Path
) -> None:
    exit_code = cli.main(["providers", "--no-persist"])
    output = capsys.readouterr().out

    assert exit_code == cli.EXIT_OK
    for name in ("gemini", "groq", "openrouter", "mock"):
        assert name in output


def test_models_command_prints_configured_models(
    capsys: pytest.CaptureFixture[str], fake_root: Path
) -> None:
    exit_code = cli.main(["models", "--provider", "groq"])
    output = capsys.readouterr().out

    assert exit_code == cli.EXIT_OK
    assert "openai/gpt-oss-120b" in output


def test_doctor_reports_a_healthy_setup(
    capsys: pytest.CaptureFixture[str], fake_root: Path
) -> None:
    exit_code = cli.main(["doctor"])
    output = capsys.readouterr().out

    assert exit_code == cli.EXIT_OK
    assert "offline route works end to end" in output
    assert "config/providers.yaml found" in output


def test_events_command_tails_the_last_run(
    capsys: pytest.CaptureFixture[str], fake_root: Path
) -> None:
    cli.main(["ask", "hi", "--provider", "mock", "--quiet"])
    capsys.readouterr()

    exit_code = cli.main(["events", "--kind", "llm.response", "-n", "5"])
    output = capsys.readouterr().out

    assert exit_code == cli.EXIT_OK
    assert "llm.response" in output


def test_unknown_agent_is_a_config_error(
    capsys: pytest.CaptureFixture[str], fake_root: Path
) -> None:
    exit_code = cli.main(["ask", "hi", "--agent", "does_not_exist"])
    captured = capsys.readouterr()

    assert exit_code == cli.EXIT_PROBLEM
    assert "Unknown agent" in captured.err


def test_clear_cooldowns_forgets_the_circuit_breaker(
    capsys: pytest.CaptureFixture[str], fake_root: Path
) -> None:
    # 1. drive the flaky provider into cooldown (it always answers 429)
    exit_code = cli.main(["ask", "hi", "--provider", "mock_flaky", "--fast", "--quiet"])
    assert exit_code == cli.EXIT_RUNTIME_ERROR
    capsys.readouterr()

    # 2. doctor must warn about it instead of failing silently
    cli.main(["doctor"])
    doctor_output = capsys.readouterr().out
    assert "cooling down" in doctor_output
    assert "--clear-cooldowns" in doctor_output

    # 3. clearing it unblocks the provider immediately
    exit_code = cli.main(["providers", "--clear-cooldowns"])
    output = capsys.readouterr().out
    assert exit_code == cli.EXIT_OK
    assert "mock_flaky" in output
    assert "No provider is on cooldown" in output

    # 4. and there is nothing left to clear
    cli.main(["providers", "--clear-cooldowns"])
    output = capsys.readouterr().out
    assert "No active cooldowns to clear" in output

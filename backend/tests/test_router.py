"""Router tests: retry, backoff, failover, quota skips and budget stops.

All of these run offline against the mock provider, so they are fast, free and
deterministic - exactly what you want from a test suite that guards the code
responsible for spending your free-tier quota.
"""

from __future__ import annotations

import pytest

from backend.core.errors import AllProvidersFailed, BudgetExceeded
from backend.core.events import EventBus, JsonlWriter
from backend.core.provider.schemas import ChatMessage
from backend.tests.helpers import (
    MISSING_KEY_ENV,
    budget_for,
    build_registry,
    build_router,
    event_kinds,
    keyed_provider,
    mock_provider,
)

MESSAGES = [ChatMessage.user("hello")]


def test_retries_a_rate_limited_provider_then_succeeds(tmp_path) -> None:
    flaky = mock_provider("flaky", fail_times=2, fail_with="rate_limit", retry_after=0.001)
    registry = build_registry([flaky])
    router, bus = build_router(
        tmp_path,
        registry,
        backoff_scale=1.0,  # scaling 1.0 + retry_after 0.001 -> a real 1 ms wait
    )

    completion = router.complete("tester_agent", MESSAGES)

    assert completion.provider == "flaky"
    assert completion.text == "mock reply"
    assert completion.attempts == [
        "flaky/m#1 -> rate_limit",
        "flaky/m#2 -> rate_limit",
        "flaky/m#3 -> ok",
    ]
    kinds = event_kinds(bus.jsonl.read_all())
    assert kinds.count("provider.retry") == 2
    assert kinds.count("provider.rate_limited") == 2
    assert kinds.count("llm.response") == 1


def test_non_retryable_error_fails_over_immediately(tmp_path) -> None:
    broken = mock_provider("broken", fail_times=999, fail_with="auth")
    healthy = mock_provider("healthy", reply="from the second provider")
    registry = build_registry([broken, healthy], routing=[("broken", "m"), ("healthy", "m")])
    router, bus = build_router(tmp_path, registry)

    completion = router.complete("tester_agent", MESSAGES)

    assert completion.provider == "healthy"
    assert completion.text == "from the second provider"
    assert completion.failed_over is True
    # a bad key gets a long cooldown, and it only cost us one request
    assert len(completion.attempts) == 2
    assert router.ledger.cooldown_remaining("broken") == pytest.approx(900, abs=2)
    assert "provider.failover" in event_kinds(bus.jsonl.read_all())


def test_all_providers_failing_raises_with_the_full_story(tmp_path) -> None:
    one = mock_provider("one", fail_times=999, fail_with="auth")
    two = mock_provider("two", fail_times=999, fail_with="server")
    registry = build_registry([one, two])
    router, _bus = build_router(tmp_path, registry)

    with pytest.raises(AllProvidersFailed) as excinfo:
        router.complete("tester_agent", MESSAGES)

    message = str(excinfo.value)
    assert "one/m#1 -> auth" in message
    assert "two/m#1 -> server" in message


def test_provider_without_a_key_is_skipped(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(MISSING_KEY_ENV, raising=False)
    needs_key = keyed_provider("needs_key")
    offline = mock_provider("offline", reply="offline answer")
    registry = build_registry([needs_key, offline])
    router, bus = build_router(tmp_path, registry)

    completion = router.complete("tester_agent", MESSAGES)

    assert completion.provider == "offline"
    records = bus.jsonl.read_all()
    skipped = [r for r in records if r["kind"] == "provider.skipped"]
    assert len(skipped) == 1
    assert "no API key" in skipped[0]["message"]
    assert needs_key.api_key_env in skipped[0]["message"]


def test_provider_at_its_daily_cap_is_skipped_not_called(tmp_path) -> None:
    limited = mock_provider("limited", limits={"requests_per_day": 1})
    healthy = mock_provider("healthy")
    registry = build_registry([limited, healthy])
    router, bus = build_router(tmp_path, registry)
    # pretend we already used the one allowed request today
    router.ledger.record_attempt("limited:m")

    completion = router.complete("tester_agent", MESSAGES)

    assert completion.provider == "healthy"
    records = bus.jsonl.read_all()
    skipped = [r for r in records if r["kind"] == "provider.skipped"]
    assert any("daily request limit" in r["message"] for r in skipped)


def test_usage_is_recorded_against_the_ledger(tmp_path) -> None:
    provider = mock_provider("p", reply="x" * 400)  # ~100 completion tokens
    registry = build_registry([provider])
    router, _bus = build_router(tmp_path, registry)

    completion = router.complete("tester_agent", MESSAGES)
    usage = router.ledger.usage("p:m")

    assert usage.requests == 1
    assert usage.tokens == completion.usage.total_tokens


def test_budget_stops_the_run(tmp_path) -> None:
    provider = mock_provider("p")
    registry = build_registry([provider])
    budget = budget_for(tmp_path, max_calls_per_run=1)
    router, _bus = build_router(tmp_path, registry, budget=budget)

    router.complete("tester_agent", MESSAGES)  # uses the only allowed call

    with pytest.raises(BudgetExceeded):
        router.complete("tester_agent", MESSAGES)


def test_overrides_replace_the_configured_chain(tmp_path) -> None:
    first = mock_provider("first")
    second = mock_provider("second", reply="forced")
    registry = build_registry([first, second])
    router, _bus = build_router(tmp_path, registry)

    spec, model = registry.resolve("second", "m")
    completion = router.complete("tester_agent", MESSAGES, override=[(spec, model)])

    assert completion.provider == "second"
    assert completion.text == "forced"


def test_events_are_written_to_the_run_log(tmp_path) -> None:
    provider = mock_provider("p")
    registry = build_registry([provider])
    events_path = tmp_path / "custom" / "events.jsonl"
    bus = EventBus(run_id="r1", jsonl=JsonlWriter(events_path), echo=False)
    router, _unused = build_router(tmp_path, registry, bus=bus)

    router.complete("tester_agent", MESSAGES)

    records = JsonlWriter(events_path).read_all()
    assert {r["run_id"] for r in records} == {"r1"}
    assert event_kinds(records)[:2] == ["provider.attempt", "llm.response"]




def test_empty_completion_is_retried_then_fails_over(tmp_path) -> None:
    # A thinking model can return HTTP 200 with no content at all. An empty
    # answer is not an answer: fail over at once and cool the model down.
    silent = mock_provider("silent", reply="")
    talker = mock_provider("talker", reply="a real answer")
    registry = build_registry([silent, talker])
    router, bus = build_router(tmp_path, registry)

    completion = router.complete("tester_agent", MESSAGES)

    assert completion.provider == "talker"
    assert completion.text == "a real answer"
    assert completion.failed_over is True
    # One attempt only: an empty completion is not transient, so the router
    # fails over at once instead of burning the same model output budget again.
    assert completion.attempts == [
        "silent/m#1 -> empty",
        "talker/m#1 -> ok",
    ]
    assert router.ledger.cooldown_reason("silent") == "empty"
    assert router.ledger.cooldown_remaining("silent") == pytest.approx(45, abs=2)
    kinds = event_kinds(bus.jsonl.read_all())
    assert kinds.count("provider.error") == 1
    assert kinds.count("provider.retry") == 0


def test_all_empty_completions_raise_all_providers_failed(tmp_path) -> None:
    registry = build_registry([mock_provider("silent", reply="")])
    router, _bus = build_router(tmp_path, registry)

    with pytest.raises(AllProvidersFailed) as excinfo:
        router.complete("tester_agent", MESSAGES)

    assert "empty" in str(excinfo.value)


def test_tokens_from_an_empty_completion_are_still_counted(tmp_path) -> None:
    # The tokens WERE spent upstream, so they must stay on the ledger and the
    # run budget - otherwise the next request would be a surprise 429.
    silent = mock_provider("silent", reply="")
    talker = mock_provider("talker", reply="ok")
    registry = build_registry([silent, talker])
    budget = budget_for(tmp_path)
    router, _bus = build_router(tmp_path, registry, budget=budget)

    router.complete("tester_agent", MESSAGES)

    assert budget.state.tokens > 0
    assert budget.state.completion_tokens > 0
    assert router.ledger.usage("silent:m").requests == 1

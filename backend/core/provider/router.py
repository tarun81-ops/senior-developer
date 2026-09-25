"""The router: retries, backoff, quota checks and failover.

Given an agent name, the router walks that agent's routing chain from
``config/agents.yaml`` until something answers. For each candidate it:

  1. checks we actually have a key (otherwise skip with a clear log line)
  2. checks the circuit breaker -- is this provider cooling down?
  3. checks our own quota ledger -- are we at the daily/per-minute ceiling?
  4. calls the provider, retrying retryable failures with exponential backoff
  5. on final failure, puts the provider on cooldown and moves to the next one

Every step emits an event, which is what makes the whole thing visible in the
terminal today and in the desktop UI in Phase 4.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from backend.core.errors import (
    AllProvidersFailed,
    ConfigError,
    EmptyResponseError,
    ProviderError,
)
from backend.core.events import EventBus
from backend.core.provider.client import OpenAICompatClient
from backend.core.provider.ratelimit import QuotaLedger, backoff_delay, cooldown_seconds
from backend.core.provider.registry import AgentConfig, Registry, RetryConfig
from backend.core.provider.schemas import ChatMessage, Completion, ModelSpec, ProviderSpec

# TYPE_CHECKING only: the runtime import would close a cycle
# orchestrator.budgets -> provider.registry -> provider.router -> budgets.
# The annotation is lazy (from __future__ import annotations), so this is enough.
if TYPE_CHECKING:
    from backend.core.orchestrator.budgets import BudgetTracker

Candidate = tuple[ProviderSpec, ModelSpec]


@dataclass
class RouterOptions:
    """Knobs the tests and the offline demo use."""

    #: multiply every computed wait by this (0 = no waiting at all)
    backoff_scale: float = 1.0
    #: sleep function, injectable so tests do not actually sleep
    sleep: Callable[[float], None] = time.sleep
    #: extra attempts allowed across the whole chain (safety net)
    max_chain_attempts: int = 12


@dataclass
class RoutingReport:
    """Everything that happened during one :meth:`ProviderRouter.complete` call."""

    agent: str
    attempts: list[str] = field(default_factory=list)
    skips: list[str] = field(default_factory=list)
    succeeded: bool = False

    def describe(self) -> str:
        lines = [f"agent '{self.agent}' routing report:"]
        lines += [f"  skipped  {item}" for item in self.skips]
        lines += [f"  tried    {item}" for item in self.attempts]
        lines.append(f"  result   {'success' if self.succeeded else 'all providers failed'}")
        return "\n".join(lines)


class ProviderRouter:
    """Turns "agent X needs an answer" into a concrete model call."""

    def __init__(
        self,
        registry: Registry,
        *,
        ledger: QuotaLedger,
        bus: EventBus,
        budget: BudgetTracker | None = None,
        client: OpenAICompatClient | None = None,
        options: RouterOptions | None = None,
    ) -> None:
        self.registry = registry
        self.ledger = ledger
        self.bus = bus
        self.budget = budget
        self.options = options or RouterOptions()
        self.client = client or OpenAICompatClient(
            timeout_seconds=registry.limits.request.timeout_seconds
        )
        self.last_report: RoutingReport | None = None

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> ProviderRouter:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- main entry point ----------------------------------------------------
    def complete(
        self,
        agent_name: str,
        messages: list[ChatMessage],
        *,
        override: list[Candidate] | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> Completion:
        agent = self.registry.agent(agent_name)
        chain = list(override) if override else self.registry.candidate_chain(agent_name)
        if not chain:
            raise ConfigError(f"Agent '{agent_name}' has an empty routing chain")

        temperature = (
            agent.temperature if temperature is None else temperature
        )
        retry = self.registry.limits.retry
        cooldowns = self.registry.limits.cooldown
        report = RoutingReport(agent=agent_name)
        self.last_report = report

        for index, (spec, model) in enumerate(chain):
            api_key = self.registry.api_key_for(spec)
            quota_key = spec.quota_key(model.id)

            if spec.requires_key and not api_key:
                self._skip(report, spec, model, f"no API key in {spec.api_key_env}")
                continue

            allowed, reason = self.ledger.check(spec, model.id)
            if not allowed:
                self._skip(report, spec, model, reason)
                continue

            budget_tokens = max_output_tokens or model.max_output_tokens
            outcome = self._try_candidate(
                agent=agent,
                spec=spec,
                model=model,
                messages=messages,
                api_key=api_key,
                quota_key=quota_key,
                temperature=temperature,
                max_output_tokens=budget_tokens,
                report=report,
                retry=retry,
                cooldowns=cooldowns,
            )
            if outcome is not None:
                report.succeeded = True
                return outcome

            if index + 1 < len(chain):
                nxt_spec, nxt_model = chain[index + 1]
                self.bus.emit(
                    "provider.failover",
                    f"giving up on this provider, next is {nxt_spec.name}/{nxt_model.id}",
                    agent=agent_name,
                    provider=spec.name,
                    model=model.id,
                )

        raise AllProvidersFailed(agent_name, [*report.skips, *report.attempts])

    # -- internals -----------------------------------------------------------
    def _skip(
        self,
        report: RoutingReport,
        spec: ProviderSpec,
        model: ModelSpec,
        reason: str,
    ) -> None:
        report.skips.append(f"{spec.name}/{model.id}: {reason}")
        self.bus.emit(
            "provider.skipped",
            reason,
            agent=report.agent,
            provider=spec.name,
            model=model.id,
        )

    def _try_candidate(
        self,
        *,
        agent: AgentConfig,
        spec: ProviderSpec,
        model: ModelSpec,
        messages: list[ChatMessage],
        api_key: str | None,
        quota_key: str,
        temperature: float,
        max_output_tokens: int,
        report: RoutingReport,
        retry,
        cooldowns,
    ) -> Completion | None:
        """Try one provider/model. Returns a Completion, or None if it is hopeless."""
        max_attempts = max(1, retry.max_attempts_per_provider)
        for attempt in range(1, max_attempts + 1):
            if len(report.attempts) >= self.options.max_chain_attempts:
                self.bus.emit(
                    "error",
                    f"stopping after {self.options.max_chain_attempts} attempts across "
                    "this routing chain (safety limit)",
                    agent=agent.name,
                )
                return None

            if self.budget is not None:
                self.budget.begin_call()

            self.bus.emit(
                "provider.attempt",
                f"attempt {attempt}/{max_attempts}",
                agent=agent.name,
                provider=spec.name,
                model=model.id,
                attempt=attempt,
                max_attempts=max_attempts,
            )

            started = time.perf_counter()
            try:
                raw = self.client.chat(
                    spec,
                    model.id,
                    messages,
                    api_key=api_key,
                    temperature=temperature,
                    max_output_tokens=max_output_tokens,
                )
            except ProviderError as exc:
                self._emit_provider_error(
                    exc,
                    spec=spec,
                    model=model,
                    quota_key=quota_key,
                    report=report,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    cooldowns=cooldowns,
                )
                if self._retry_or_stop(
                    exc,
                    agent=agent,
                    spec=spec,
                    model=model,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    retry=retry,
                ):
                    continue
                return None

            latency_ms = raw.latency_ms or int((time.perf_counter() - started) * 1000)
            usage = raw.usage
            if not raw.text.strip():
                # An empty answer is not an answer: "thinking" models can spend
                # the whole output budget on internal reasoning and return an
                # empty `content` (observed live on OpenRouter qwen3.8: 4096
                # output tokens, zero text, 108 s). The tokens WERE spent, so
                # they stay on the ledger and the budget - counted ONCE here,
                # which is why _emit_provider_error skips its own request slot.
                self.ledger.record_attempt(quota_key, tokens=usage.total_tokens)
                if self.budget is not None:
                    self.budget.add_usage(
                        prompt_tokens=usage.prompt_tokens,
                        completion_tokens=usage.completion_tokens,
                    )
                empty = EmptyResponseError(
                    "empty completion "
                    f"(finish_reason={raw.finish_reason or 'unknown'}, "
                    f"{usage.completion_tokens} output tokens spent)",
                    provider=spec.name,
                    model=model.id,
                    status=200,
                )
                self._emit_provider_error(
                    empty,
                    spec=spec,
                    model=model,
                    quota_key=quota_key,
                    report=report,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    cooldowns=cooldowns,
                    record_request=False,
                )
                if self._retry_or_stop(
                    empty,
                    agent=agent,
                    spec=spec,
                    model=model,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    retry=retry,
                ):
                    continue
                return None

            self.ledger.record_attempt(quota_key, tokens=usage.total_tokens)
            if self.budget is not None:
                self.budget.add_usage(
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                )
            report.attempts.append(f"{spec.name}/{model.id}#{attempt} -> ok")
            failed_over = bool(report.skips) or len(report.attempts) > 1
            self.bus.emit(
                "llm.response",
                f"{latency_ms} ms, {usage.total_tokens} tokens"
                + (" (after failover)" if failed_over else ""),
                agent=agent.name,
                provider=spec.name,
                model=model.id,
                tokens=usage.total_tokens,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                latency_ms=latency_ms,
                failed_over=failed_over,
                preview=raw.text[:160],
            )
            return Completion(
                text=raw.text,
                provider=spec.name,
                model=model.id,
                usage=usage,
                latency_ms=latency_ms,
                attempts=list(report.attempts),
                failed_over=failed_over,
                finish_reason=raw.finish_reason,
            )
        return None

    def _retry_or_stop(
        self,
        exc: ProviderError,
        *,
        agent: AgentConfig,
        spec: ProviderSpec,
        model: ModelSpec,
        attempt: int,
        max_attempts: int,
        retry: RetryConfig,
    ) -> bool:
        """True when this candidate deserves another attempt after ``exc``.

        Shared by the HTTP-failure path and the empty-completion path, so both
        retry with the same exponential backoff and emit the same event.
        """
        if not exc.retryable or attempt >= max_attempts:
            return False
        if exc.kind == "empty":
            # Not transient: the model's output budget went to internal
            # reasoning. Retrying the same model just burns more tokens, so
            # fail over to the next provider immediately.
            return False
        delay = backoff_delay(
            attempt, retry, retry_after=exc.retry_after, scale=self.options.backoff_scale
        )
        if delay > 0:
            self.bus.emit(
                "provider.retry",
                f"waiting {delay:.1f}s before attempt {attempt + 1}/{max_attempts}",
                agent=agent.name,
                provider=spec.name,
                model=model.id,
                delay_seconds=round(delay, 2),
                error_kind=exc.kind,
            )
            self.options.sleep(delay)
        return True

    def _emit_provider_error(
        self,
        exc: ProviderError,
        *,
        spec: ProviderSpec,
        model: ModelSpec,
        quota_key: str,
        report: RoutingReport,
        attempt: int,
        max_attempts: int,
        cooldowns,
        record_request: bool = True,
    ) -> None:
        # A failed request still consumes a request slot on the free tier, so it
        # is counted against our own RPM/RPD ledger too - unless the caller
        # already accounted for this call (the empty-completion path does).
        if record_request:
            self.ledger.record_attempt(quota_key)
        seconds = cooldown_seconds(exc.kind, cooldowns)
        if seconds > 0:
            self.ledger.set_cooldown(spec.name, reason=exc.kind, seconds=seconds)
        event_kind = "provider.rate_limited" if exc.kind == "rate_limit" else "provider.error"
        self.bus.emit(
            event_kind,
            f"{exc.kind} on attempt {attempt}/{max_attempts}: {exc}"
            + (f" (cooldown {seconds}s)" if seconds else ""),
            agent=report.agent,
            provider=spec.name,
            model=model.id,
            error_kind=exc.kind,
            status=exc.status,
            retryable=exc.retryable,
            retry_after=exc.retry_after,
            cooldown_seconds=seconds,
        )
        report.attempts.append(f"{spec.name}/{model.id}#{attempt} -> {exc.kind}")



"""Exception hierarchy.

Two big groups matter:

``RetryableError``   -> try again (maybe after a wait). Rate limits, 5xx, network.
``NonRetryableError``-> do not try again (bad key, bad model id, no credits).

The router only needs to know which group an error belongs to, plus a
``kind`` string that maps to a cooldown length in ``config/limits.yaml``.
"""

from __future__ import annotations


class AgentSystemError(Exception):
    """Base class for every error this project raises on purpose."""


class ConfigError(AgentSystemError):
    """The YAML config or the environment is wrong."""


class MissingApiKey(AgentSystemError):
    """A provider needs a key that is not configured yet."""

    def __init__(self, provider: str, env_var: str) -> None:
        super().__init__(
            f"No API key for provider '{provider}'. Set the environment variable "
            f"{env_var} (see .env.example) or run the 'doctor' command."
        )
        self.provider = provider
        self.env_var = env_var


class ProviderError(AgentSystemError):
    """Anything that goes wrong while talking to a provider."""

    kind: str = "unknown"
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        model: str | None = None,
        status: int | None = None,
        retry_after: float | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.model = model
        self.status = status
        self.retry_after = retry_after
        self.body = (body or "")[:500]

    @property
    def summary(self) -> str:
        where = f"{self.provider}/{self.model}" if self.model else self.provider
        status = f" HTTP {self.status}" if self.status else ""
        return f"{where}{status}: {self}"


class RateLimitError(ProviderError):
    """HTTP 429 - too many requests, or a daily quota is exhausted."""

    kind = "rate_limit"
    retryable = True


class AuthError(ProviderError):
    """HTTP 401/403 - missing, wrong or revoked key."""

    kind = "auth"
    retryable = False


class InsufficientCreditsError(ProviderError):
    """HTTP 402 - the account has no credit left."""

    kind = "credits"
    retryable = False


class ModelNotFoundError(ProviderError):
    """HTTP 404 - the model id in the config does not exist."""

    kind = "model_not_found"
    retryable = False


class BadRequestError(ProviderError):
    """HTTP 400 - the request we built is invalid (our bug, not theirs)."""

    kind = "bad_request"
    retryable = False


class ServerError(ProviderError):
    """HTTP 5xx - their side is broken; retry later."""

    kind = "server"
    retryable = True


class NetworkError(ProviderError):
    """Timeouts and connection failures."""

    kind = "network"
    retryable = True


class ProviderUnavailable(ProviderError):
    """Local pre-flight failure: missing key, cooldown, quota exhausted."""

    kind = "unavailable"
    retryable = False


class AllProvidersFailed(AgentSystemError):
    """Every candidate in an agent's routing chain failed."""

    def __init__(self, agent: str, attempts: list[str]) -> None:
        detail = "\n  - " + "\n  - ".join(attempts) if attempts else " (no candidates)"
        super().__init__(f"All providers failed for agent '{agent}':{detail}")
        self.agent = agent
        self.attempts = attempts


class BudgetExceeded(AgentSystemError):
    """A hard per-run ceiling was hit; a human must decide what happens next."""

    def __init__(self, message: str, *, calls: int, tokens: int) -> None:
        super().__init__(f"{message} (calls used: {calls}, tokens used: {tokens})")
        self.calls = calls
        self.tokens = tokens

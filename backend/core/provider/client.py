"""The single HTTP code path that talks to every provider.

Gemini, Groq and OpenRouter all expose the same OpenAI-compatible endpoint:

    POST {base_url}/chat/completions
    Authorization: Bearer <api key>
    {"model": ..., "messages": [...], "temperature": ..., "max_tokens": ...}

Because of that, adding a provider is a YAML edit, not a code change.

The mock provider is handled here too. It is not a gimmick: it lets the whole
system (and the test suite) exercise retries, backoff, cooldown and failover
without a key, without network access and without spending free quota.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from backend.core.errors import (
    AuthError,
    BadRequestError,
    InsufficientCreditsError,
    ModelNotFoundError,
    NetworkError,
    ProviderError,
    RateLimitError,
    ServerError,
)
from backend.core.provider.schemas import ChatMessage, ProviderSpec, Usage

#: Maps a mock's configured failure name onto a real exception class.
_MOCK_ERRORS: dict[str, type[ProviderError]] = {
    "rate_limit": RateLimitError,
    "auth": AuthError,
    "credits": InsufficientCreditsError,
    "server": ServerError,
    "network": NetworkError,
    "model_not_found": ModelNotFoundError,
    "bad_request": BadRequestError,
}


@dataclass
class RawCompletion:
    """What came back from the wire, before the router decorates it."""

    text: str
    usage: Usage
    finish_reason: str | None = None
    latency_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 characters per token) for the mock provider."""
    return max(1, len(text) // 4)


class OpenAICompatClient:
    """Synchronous, retry-free HTTP client.

    Retries and failover live in :mod:`backend.core.provider.router`, so this
    class stays a thin, predictable wrapper. Synchronous on purpose: far easier
    to read and debug; the FastAPI layer can call it in a worker thread.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(timeout=timeout_seconds, transport=transport)
        #: per-process call counter used by the mock provider to simulate failures
        self._mock_calls: dict[str, int] = {}

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> OpenAICompatClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- public API ---------------------------------------------------------
    def chat(
        self,
        spec: ProviderSpec,
        model_id: str,
        messages: list[ChatMessage],
        *,
        api_key: str | None,
        temperature: float = 0.2,
        max_output_tokens: int = 4096,
    ) -> RawCompletion:
        if spec.kind == "mock":
            return self._mock_chat(spec, model_id, messages)
        if not api_key:
            raise AuthError(
                "No API key available for this provider", provider=spec.name, model=model_id
            )

        payload: dict[str, Any] = {
            "model": model_id,
            "messages": [m.as_payload() for m in messages],
            "temperature": temperature,
            "max_tokens": max_output_tokens,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            **spec.extra_headers,
        }
        url = f"{spec.base_url.rstrip('/')}/chat/completions"

        started = time.perf_counter()
        try:
            response = self._client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise NetworkError(
                f"Request timed out after {self._client.timeout}: {exc}",
                provider=spec.name,
                model=model_id,
            ) from exc
        except httpx.HTTPError as exc:
            raise NetworkError(
                f"Connection failed: {exc}", provider=spec.name, model=model_id
            ) from exc
        latency_ms = int((time.perf_counter() - started) * 1000)

        if response.status_code >= 400:
            raise self._classify(spec, model_id, response)

        data = self._safe_json(response, spec, model_id)
        if "error" in data and not data.get("choices"):
            raise self._classify(spec, model_id, response, body=str(data.get("error")))

        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(
                "Provider returned no choices",
                provider=spec.name,
                model=model_id,
                status=response.status_code,
                body=response.text,
            )
        choice = choices[0] or {}
        message = choice.get("message") or {}
        text = message.get("content") or ""
        if isinstance(text, list):  # some gateways return content parts
            text = "".join(part.get("text", "") for part in text if isinstance(part, dict))

        return RawCompletion(
            text=str(text).strip(),
            usage=Usage.from_api(data.get("usage")),
            finish_reason=choice.get("finish_reason"),
            latency_ms=latency_ms,
            raw=data,
        )

    def list_models(self, spec: ProviderSpec, *, api_key: str | None) -> list[str]:
        """Ask a provider which models it offers (used by ``cli models``)."""
        if spec.kind == "mock":
            return sorted(spec.models)
        if not api_key:
            raise AuthError("No API key available for this provider", provider=spec.name)
        url = f"{spec.base_url.rstrip('/')}/models"
        headers = {"Authorization": f"Bearer {api_key}", **spec.extra_headers}
        try:
            response = self._client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise NetworkError(f"Connection failed: {exc}", provider=spec.name) from exc
        if response.status_code >= 400:
            raise self._classify(spec, None, response)
        data = self._safe_json(response, spec, None)
        entries = data.get("data") or data.get("models") or []
        ids: list[str] = []
        for entry in entries:
            if isinstance(entry, dict):
                model_id = entry.get("id") or entry.get("name")
                if model_id:
                    ids.append(str(model_id))
        return sorted(ids)

    # -- internals ----------------------------------------------------------
    def _safe_json(
        self, response: httpx.Response, spec: ProviderSpec, model_id: str | None
    ) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(
                "Provider returned a non-JSON response (maybe a proxy or login page)",
                provider=spec.name,
                model=model_id,
                status=response.status_code,
                body=response.text,
            ) from exc
        return data if isinstance(data, dict) else {"data": data}

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> float | None:
        """Read ``Retry-After``; providers send either seconds or an HTTP date."""
        raw = response.headers.get("retry-after")
        if not raw:
            return None
        raw = raw.strip()
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
        try:
            from email.utils import parsedate_to_datetime

            when = parsedate_to_datetime(raw)
            if when is not None:
                from datetime import datetime, timezone

                delta = when - datetime.now(timezone.utc)
                return max(0.0, delta.total_seconds())
        except (TypeError, ValueError):
            return None
        return None

    @staticmethod
    def _error_message(response: httpx.Response, body: str) -> str:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"])
            if isinstance(error, str) and error:
                return error
            for key in ("message", "detail", "error_description"):
                if payload.get(key):
                    return str(payload[key])
        return body[:300] or f"HTTP {response.status_code}"

    def _classify(
        self,
        spec: ProviderSpec,
        model_id: str | None,
        response: httpx.Response,
        *,
        body: str | None = None,
    ) -> ProviderError:
        """Turn an HTTP status into the right exception class + cooldown kind."""
        status = response.status_code
        text = body if body is not None else response.text
        message = self._error_message(response, text)
        common = {
            "provider": spec.name,
            "model": model_id,
            "status": status,
            "retry_after": self._parse_retry_after(response),
            "body": text,
        }
        if status == 429:
            return RateLimitError(message, **common)
        if status in (401, 403):
            return AuthError(message, **common)
        if status == 402:
            return InsufficientCreditsError(message, **common)
        if status == 404:
            return ModelNotFoundError(message, **common)
        if status == 408:
            return NetworkError(message, **common)
        if status == 400:
            return BadRequestError(message, **common)
        if 500 <= status < 600:
            return ServerError(message, **common)
        return ProviderError(message, **common)

    # -- mock provider ------------------------------------------------------
    def reset_mock(self) -> None:
        """Forget simulated mock failures (used between tests / demo runs)."""
        self._mock_calls.clear()

    def _mock_chat(
        self, spec: ProviderSpec, model_id: str, messages: list[ChatMessage]
    ) -> RawCompletion:
        from backend.core.provider.schemas import MockBehaviour

        behaviour = spec.mock or MockBehaviour()
        calls = self._mock_calls.get(spec.name, 0) + 1
        self._mock_calls[spec.name] = calls

        if calls <= behaviour.fail_times:
            error_cls = _MOCK_ERRORS.get(behaviour.fail_with, RateLimitError)
            status_by_kind = {
                "rate_limit": 429,
                "auth": 401,
                "credits": 402,
                "server": 503,
                "model_not_found": 404,
                "bad_request": 400,
            }
            raise error_cls(
                f"simulated {behaviour.fail_with} (mock call #{calls})",
                provider=spec.name,
                model=model_id,
                status=status_by_kind.get(behaviour.fail_with),
                retry_after=behaviour.retry_after,
            )

        prompt_tokens = sum(estimate_tokens(m.content) for m in messages)
        completion_tokens = estimate_tokens(behaviour.reply)
        return RawCompletion(
            text=behaviour.reply,
            usage=Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
            finish_reason="stop",
            latency_ms=1,
            raw={"provider": "mock", "model": model_id},
        )



"""HTTP client tests.

These use ``httpx.MockTransport``, so the real request/response plumbing is
exercised (headers, JSON payload, status classification, ``Retry-After``
parsing) without any network access.
"""

from __future__ import annotations

import httpx
import pytest

from backend.core.errors import (
    AuthError,
    InsufficientCreditsError,
    NetworkError,
    ProviderError,
    RateLimitError,
    ServerError,
)
from backend.core.provider.client import OpenAICompatClient
from backend.core.provider.schemas import ChatMessage, ProviderSpec

MESSAGES = [ChatMessage.system("be nice"), ChatMessage.user("hello")]


def provider(**overrides) -> ProviderSpec:
    payload = {
        "name": "test",
        "kind": "openai_compatible",
        "base_url": "https://api.example/v1/",
        "api_key_env": "UNIT_TEST_KEY",
        "models": {"m": {"id": "m"}},
    }
    payload.update(overrides)
    return ProviderSpec.model_validate(payload)


def client(handler) -> OpenAICompatClient:
    return OpenAICompatClient(timeout_seconds=5, transport=httpx.MockTransport(handler))


def test_successful_call_parses_text_usage_and_request_shape() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.read().decode()
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "  hello world  "},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            },
        )

    with client(handler) as api:
        raw = api.chat(provider(), "m", MESSAGES, api_key="secret", temperature=0.3)

    assert seen["url"] == "https://api.example/v1/chat/completions"
    assert seen["auth"] == "Bearer secret"
    assert '"model":"m"' in seen["body"]
    assert '"stream":false' in seen["body"]
    assert raw.text == "hello world"
    assert raw.usage.total_tokens == 18
    assert raw.finish_reason == "stop"


def test_extra_headers_are_merged() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["title"] = request.headers.get("x-title")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    spec = provider(extra_headers={"X-Title": "senior-developer-agents"})
    with client(handler) as api:
        api.chat(spec, "m", MESSAGES, api_key="k")

    assert seen["title"] == "senior-developer-agents"


def test_429_is_a_retryable_rate_limit_with_retry_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"retry-after": "12"},
            json={"error": {"message": "Rate limit exceeded"}},
        )

    with client(handler) as api, pytest.raises(RateLimitError) as excinfo:
        api.chat(provider(), "m", MESSAGES, api_key="k")

    error = excinfo.value
    assert error.kind == "rate_limit"
    assert error.retryable is True
    assert error.retry_after == 12.0
    assert "Rate limit exceeded" in str(error)
    assert error.status == 429


def test_retry_after_as_http_date_is_parsed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"retry-after": "Wed, 21 Oct 2099 07:28:00 GMT"},
            json={"error": "slow down"},
        )

    with client(handler) as api, pytest.raises(RateLimitError) as excinfo:
        api.chat(provider(), "m", MESSAGES, api_key="k")

    assert excinfo.value.retry_after is not None
    assert excinfo.value.retry_after > 0


@pytest.mark.parametrize(
    ("status", "expected_kind", "retryable"),
    [
        (401, "auth", False),
        (403, "auth", False),
        (402, "credits", False),
        (404, "model_not_found", False),
        (400, "bad_request", False),
        (500, "server", True),
        (503, "server", True),
    ],
)
def test_status_codes_map_to_error_kinds(status: int, expected_kind: str, retryable: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "nope"}})

    with client(handler) as api, pytest.raises(ProviderError) as excinfo:
        api.chat(provider(), "m", MESSAGES, api_key="k")

    assert excinfo.value.kind == expected_kind
    assert excinfo.value.retryable is retryable


def test_transport_failure_becomes_a_retryable_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with client(handler) as api, pytest.raises(NetworkError) as excinfo:
        api.chat(provider(), "m", MESSAGES, api_key="k")

    assert excinfo.value.retryable is True


def test_missing_key_is_an_auth_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        return httpx.Response(200, json={})

    with client(handler) as api, pytest.raises(AuthError):
        api.chat(provider(), "m", MESSAGES, api_key=None)


def test_402_credits_message_is_kept() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"error": {"message": "Insufficient credits"}})

    with client(handler) as api, pytest.raises(InsufficientCreditsError) as excinfo:
        api.chat(provider(), "m", MESSAGES, api_key="k")

    assert "Insufficient credits" in str(excinfo.value)


def test_response_without_choices_is_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "abc"})

    with client(handler) as api, pytest.raises(ProviderError) as excinfo:
        api.chat(provider(), "m", MESSAGES, api_key="k")

    assert "no choices" in str(excinfo.value)


def test_404_on_list_models_is_a_model_not_found_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    with client(handler) as api, pytest.raises(ProviderError) as excinfo:
        api.list_models(provider(), api_key="k")

    assert excinfo.value.kind == "model_not_found"


def test_error_classes_have_the_expected_hierarchy() -> None:
    assert issubclass(ServerError, ProviderError)
    assert issubclass(RateLimitError, ProviderError)
    assert issubclass(AuthError, ProviderError)


"""Sanitized provider errors and retry-visible status categories."""

from __future__ import annotations

import json
import threading

import httpx
import pytest

from call_attempt_state import CallAttemptState
from config_schema import AgentConfig, ResolvedProviderConnection
from llm_client import LLMClient
from model_registry import ModelMetadata, ModelRecord, RunModel
from providers._errors import LLMContextOverflowError, LLMError, LLMPermanentError


def _client(
    adapter: str,
    handler,
    *,
    cancel_event: threading.Event | None = None,
) -> LLMClient:
    """Build a local mock-transport client for one supported adapter."""
    model = ModelRecord(
        f"connection/{adapter}-model",
        "connection",
        f"{adapter}-model",
        "Test",
        ModelMetadata(context_tokens=8192, output_tokens=1024),
    )
    base_urls = {
        "openai": "https://openai.example.test/v1",
        "openrouter": "https://openrouter.example.test/api/v1",
        "google": "https://google.example.test/v1beta",
        "anthropic": "https://anthropic.example.test",
        "ollama": "https://ollama.com",
    }
    connection = ResolvedProviderConnection(
        "connection", adapter, base_urls[adapter], 5, "fake-provider-secret"
    )
    return LLMClient(
        RunModel(model, connection, 1, 1),
        agent_config=AgentConfig(ctx_max_tokens=4096),
        http_transport=httpx.MockTransport(handler),
        cancel_event=cancel_event,
    )


def _request(client: LLMClient) -> str:
    """Send one text request through the production adapter and transport."""
    return client.chat([{"role": "user", "content": "user-request-secret"}])


@pytest.mark.parametrize("adapter", ["openai", "google", "anthropic"])
def test_unknown_http_200_api_error_never_exposes_provider_values(adapter: str) -> None:
    secret = "endpoint-secret-provider-secret"
    client = _client(
        adapter,
        lambda request: httpx.Response(
            200,
            json={"error": {"code": secret, "type": secret, "message": secret}},
        ),
    )
    state = CallAttemptState(1, 0)
    try:
        with client.bind_attempt(state), pytest.raises(LLMError) as caught:
            _request(client)
        assert state.outbound_attempts == 1
        assert not isinstance(caught.value, LLMPermanentError)
        assert secret not in str(caught.value)
        assert "fake-provider-secret" not in str(caught.value)
        assert "user-request-secret" not in str(caught.value)
    finally:
        client.close()


@pytest.mark.parametrize(
    ("adapter", "payload"),
    [
        ("openai", {"error": {"code": "invalid_api_key"}}),
        ("google", {"error": {"status": "PERMISSION_DENIED"}}),
        ("anthropic", {"error": {"type": "invalid_api_key"}}),
    ],
)
def test_allowlisted_provider_error_code_remains_permanent_and_safe(adapter, payload) -> None:
    client = _client(adapter, lambda request: httpx.Response(200, json=payload))
    state = CallAttemptState(1, 0)
    try:
        with client.bind_attempt(state), pytest.raises(
            LLMPermanentError, match="invalid_api_key|permission_denied"
        ):
            _request(client)
        assert state.outbound_attempts == 1
    finally:
        client.close()


@pytest.mark.parametrize("adapter", ["openai", "google", "anthropic"])
@pytest.mark.parametrize("status", [400, 413])
def test_http_context_overflow_keeps_only_safe_marker(adapter: str, status: int) -> None:
    body = '{"error":{"message":"context_length_exceeded provider-secret"}}'
    client = _client(adapter, lambda request: httpx.Response(status, text=body))
    state = CallAttemptState(1, 0)
    try:
        with client.bind_attempt(state), pytest.raises(LLMContextOverflowError) as caught:
            _request(client)
        assert state.outbound_attempts == 1
        assert caught.value.status_code == status
        assert str(caught.value) == "Provider rejected request: context_length_exceeded"
        assert caught.value.__cause__ is None
        assert not hasattr(caught.value, "response") and not hasattr(caught.value, "request")
        from react_loop import _classify_llm_error

        info = _classify_llm_error(caught.value)
        assert info.type == "context" and info.retryable is False
        assert "provider-secret" not in str(caught.value)
        assert "fake-provider-secret" not in str(caught.value)
        assert "user-request-secret" not in str(caught.value)
    finally:
        client.close()


@pytest.mark.parametrize("adapter", ["openai", "google", "anthropic"])
@pytest.mark.parametrize("status", [429, 503])
def test_retryable_http_status_is_preserved_without_response_secrets(adapter: str, status: int) -> None:
    client = _client(
        adapter,
        lambda request: httpx.Response(status, text="provider-secret response-secret"),
    )
    state = CallAttemptState(1, 0)
    try:
        with client.bind_attempt(state), pytest.raises(httpx.HTTPStatusError) as caught:
            _request(client)
        assert state.outbound_attempts == 1
        assert caught.value.response.status_code == status
        assert caught.value.response.text == ""
        assert "provider-secret" not in str(caught.value)
        assert "response-secret" not in str(caught.value)
        assert "fake-provider-secret" not in str(caught.value)
    finally:
        client.close()


def test_ollama_context_overflow_and_status_errors_are_sanitized() -> None:
    for status, text, expected_text in [
        (400, "context_length_exceeded provider-secret", "context_length_exceeded"),
        (413, "context_length_exceeded provider-secret", "context_length_exceeded"),
        (429, "provider-secret", ""),
        (503, "provider-secret", ""),
    ]:
        client = _client(
            "ollama",
            lambda request, status=status, text=text: httpx.Response(status, text=text),
        )
        state = CallAttemptState(1, 0)
        try:
            with client.bind_attempt(state), pytest.raises(httpx.HTTPStatusError) as caught:
                _request(client)
            assert state.outbound_attempts == 1
            assert caught.value.response.status_code == status
            assert caught.value.response.text == expected_text
            assert "provider-secret" not in str(caught.value)
            assert "fake-provider-secret" not in str(caught.value)
            assert "user-request-secret" not in str(caught.value)
        finally:
            client.close()


@pytest.mark.parametrize("adapter", ["openai", "google", "anthropic", "ollama"])
def test_connection_errors_are_sanitized_and_remain_transport_errors(adapter: str) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("endpoint-secret provider-secret", request=request)

    client = _client(adapter, fail)
    state = CallAttemptState(1, 0)
    try:
        with client.bind_attempt(state), pytest.raises(httpx.ConnectError) as caught:
            _request(client)
        assert state.outbound_attempts == 1
        assert "endpoint-secret" not in str(caught.value)
        assert "provider-secret" not in str(caught.value)
        assert "fake-provider-secret" not in str(caught.value)
        assert "user-request-secret" not in str(caught.value)
        assert str(caught.value.request.url) == "https://provider.invalid/"
    finally:
        client.close()


def test_ollama_sdk_read_timeout_is_sanitized_with_timeout_category() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("endpoint-secret body-secret", request=request)

    client = _client("ollama", timeout)
    state = CallAttemptState(1, 0)
    try:
        with client.bind_attempt(state), pytest.raises(httpx.ReadTimeout) as caught:
            _request(client)
        assert state.outbound_attempts == 1
        assert "endpoint-secret" not in str(caught.value)
        assert "body-secret" not in str(caught.value)
        assert "fake-provider-secret" not in str(caught.value)
        assert str(caught.value.request.url) == "https://provider.invalid/"
    finally:
        client.close()


@pytest.mark.parametrize("error_type", [httpx.ReadError, httpx.WriteError])
@pytest.mark.parametrize("native", [False, True])
def test_ollama_sdk_request_errors_detach_original_request(
    error_type: type[httpx.RequestError], native: bool
) -> None:
    captured: list[httpx.Request] = []

    def fail(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        raise error_type("transport-secret", request=request)

    client = _client("ollama", fail)
    state = CallAttemptState(1, 0)
    messages = [{"role": "user", "content": "prompt-secret"}]
    try:
        with client.bind_attempt(state), pytest.raises(error_type) as caught:
            if native:
                client.chat_with_tools(messages, [])
            else:
                client.chat(messages)

        assert len(captured) == 1
        assert state.outbound_attempts == 1
        assert str(caught.value) == "Provider transport failed"
        safe_request = caught.value.request
        assert str(safe_request.url) == "https://provider.invalid/"
        assert "authorization" not in safe_request.headers
        assert "fake-provider-secret" not in safe_request.content.decode("utf-8", errors="ignore")
        assert "prompt-secret" not in safe_request.content.decode("utf-8", errors="ignore")
    finally:
        client.close()


def test_cancellation_does_not_start_ollama_outbound_attempt() -> None:
    captured: list[httpx.Request] = []
    cancel = threading.Event()
    client = _client(
        "ollama",
        lambda request: captured.append(request) or httpx.Response(200),
        cancel_event=cancel,
    )
    state = CallAttemptState(1, 0)
    with client.bind_attempt(state):
        cancel.set()
        with pytest.raises(Exception, match="cancelled"):
            client.chat_with_tools([{"role": "user", "content": "hello"}], [])
    assert state.outbound_attempts == 0
    assert captured == []
    client.close()


def test_unexpected_sdk_transport_exception_propagates_without_reclassification() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("unexpected-test-error")

    client = _client("ollama", fail)
    state = CallAttemptState(1, 0)
    try:
        with client.bind_attempt(state), pytest.raises(
            RuntimeError, match="unexpected-test-error"
        ):
            _request(client)
        assert state.outbound_attempts == 1
    finally:
        client.close()


def test_safe_status_exception_does_not_retain_original_headers_or_body() -> None:
    client = _client(
        "openai",
        lambda request: httpx.Response(
            400,
            text=json.dumps({"error": {"code": "invalid_api_key", "message": "body-secret"}}),
            headers={"x-response-secret": "header-secret"},
        ),
    )
    try:
        with pytest.raises(LLMPermanentError) as caught:
            _request(client)
        assert "body-secret" not in str(caught.value)
        assert "header-secret" not in str(caught.value)
        assert "fake-provider-secret" not in str(caught.value)
    finally:
        client.close()

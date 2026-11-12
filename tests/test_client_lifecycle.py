"""Public pinned-client call lifecycle logging behavior."""

from __future__ import annotations

import threading
from typing import Any

import httpx
import pytest

import llm_client
from agent_logging import LogEvent
from interfaces import ChatResponse
from model_registry import ModelMetadata
from providers._errors import (
    LLMCancelledError,
    LLMEmptyResponseError,
    LLMError,
    LLMPermanentError,
)
from tests.run_model_fixtures import make_run_model_client


def _response(*, native: bool = False) -> httpx.Response:
    """Return a valid OpenAI-compatible response for the selected mode."""
    message: dict[str, Any] = {"content": "ok"}
    if native:
        message = {
            "content": None,
            "tool_calls": [{
                "id": "call-1",
                "function": {"name": "lookup", "arguments": "{}"},
            }],
        }
    return httpx.Response(
        200,
        json={
            "choices": [{"message": message}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        },
    )


def _client(
    handler,
    *,
    metadata: ModelMetadata | None = None,
    cancel_event: threading.Event | None = None,
    adapter: str = "openai",
):
    """Build a pinned mock-transport client with zero client retries."""
    return make_run_model_client(
        adapter,
        httpx.MockTransport(handler),
        metadata=metadata or ModelMetadata(context_tokens=8192, output_tokens=1024),
        upstream_id="lifecycle-test-model",
        cancel_event=cancel_event,
    )


def _capture_events(monkeypatch) -> list[tuple[LogEvent, str, dict[str, Any]]]:
    events: list[tuple[LogEvent, str, dict[str, Any]]] = []
    monkeypatch.setattr(
        llm_client.agent_logging,
        "log_event",
        lambda event, message, **fields: events.append((event, message, fields)),
    )
    return events


@pytest.mark.parametrize("native", [False, True], ids=["text-wrapper", "native-wrapper"])
def test_successful_public_wrappers_emit_one_call_and_preserve_usage(monkeypatch, native):
    requests: list[httpx.Request] = []
    client = _client(
        lambda request: requests.append(request) or _response(native=native)
    )
    events = _capture_events(monkeypatch)
    try:
        if native:
            response = client.chat_with_tools_fallback(
                [{"role": "user", "content": "request"}], tools=[]
            )
            assert isinstance(response, ChatResponse)
            assert response.tool_calls and response.tool_calls[0].name == "lookup"
        else:
            assert client.chat_with_fallback(
                [{"role": "user", "content": "request"}]
            ) == "ok"

        assert len(requests) == 1
        assert [event for event, _message, _fields in events] == [LogEvent.LLM_CALL]
        assert events[0][2]["model"] == "lifecycle-test-model"
        assert client.get_today_usage()["prompt_tokens"] == 3
        assert client.get_today_usage()["completion_tokens"] == 2
    finally:
        client.close()


@pytest.mark.parametrize(
    ("response", "expected_error", "safe_category"),
    [
        (httpx.Response(200, text="malformed-json"), LLMError, "provider_error"),
        (httpx.Response(200, json={}), LLMEmptyResponseError, "empty_response"),
    ],
    ids=["malformed-json", "empty-provider-response"],
)
def test_provider_response_failures_are_logged_after_one_send(
    monkeypatch, response, expected_error, safe_category
):
    requests: list[httpx.Request] = []
    client = _client(lambda request: requests.append(request) or response)
    events = _capture_events(monkeypatch)
    try:
        with pytest.raises(expected_error):
            client.chat([{"role": "user", "content": "request"}])

        assert len(requests) == 1
        assert [event for event, _message, _fields in events] == [
            LogEvent.LLM_CALL, LogEvent.LLM_FAILED
        ]
        assert events[1][2]["err"] == safe_category
        assert isinstance(events[1][2]["dur_ms"], int)
        assert events[1][2]["dur_ms"] >= 0
    finally:
        client.close()


def test_result_type_validation_is_inside_failure_boundary(monkeypatch):
    client = _client(lambda _request: pytest.fail("transport must not be called"))
    events = _capture_events(monkeypatch)
    monkeypatch.setattr(client, "_invoke_provider", lambda *_args, **_kwargs: object())
    try:
        with pytest.raises(LLMError, match="unexpected response"):
            client.chat([{"role": "user", "content": "request"}])
        assert [event for event, _message, _fields in events] == [
            LogEvent.LLM_CALL, LogEvent.LLM_FAILED
        ]
        assert events[1][2]["err"] == "provider_error"
    finally:
        client.close()


def test_unsupported_native_call_has_no_lifecycle_event_or_send(monkeypatch):
    requests: list[httpx.Request] = []
    client = _client(
        lambda request: requests.append(request) or httpx.Response(200, json={}),
        adapter="anthropic",
    )
    events = _capture_events(monkeypatch)
    try:
        with pytest.raises(NotImplementedError, match="unsupported"):
            client.chat_with_tools([{"role": "user", "content": "request"}], [])
        assert requests == []
        assert events == []
    finally:
        client.close()


def test_cancelled_public_call_emits_failure_without_outbound_attempt(monkeypatch):
    requests: list[httpx.Request] = []
    cancel_event = threading.Event()
    cancel_event.set()
    client = _client(
        lambda request: requests.append(request) or httpx.Response(200, json={}),
        cancel_event=cancel_event,
    )
    events = _capture_events(monkeypatch)
    try:
        with pytest.raises(LLMCancelledError):
            client.chat([{"role": "user", "content": "request"}])
        assert requests == []
        assert [event for event, _message, _fields in events] == [
            LogEvent.LLM_CALL, LogEvent.LLM_FAILED
        ]
        assert events[1][2]["err"] == "cancelled"
    finally:
        client.close()


def test_permanent_status_emits_failure_after_single_outbound_attempt(monkeypatch):
    requests: list[httpx.Request] = []
    client = _client(
        lambda request: requests.append(request)
        or httpx.Response(400, json={"error": {"code": "invalid_api_key"}})
    )
    events = _capture_events(monkeypatch)
    try:
        with pytest.raises(llm_client.LLMPermanentError):
            client.chat([{"role": "user", "content": "request"}])
        assert len(requests) == 1
        assert [event for event, _message, _fields in events] == [
            LogEvent.LLM_CALL, LogEvent.LLM_FAILED
        ]
        assert events[1][2]["err"] == "permanent"
    finally:
        client.close()


def test_each_public_retry_invocation_gets_its_own_event_pair(monkeypatch):
    requests: list[httpx.Request] = []
    client = _client(
        lambda request: requests.append(request) or httpx.Response(503, text="private body")
    )
    events = _capture_events(monkeypatch)
    try:
        for _ in range(2):
            with pytest.raises(httpx.HTTPStatusError):
                client.chat_with_fallback([{"role": "user", "content": "request"}])
        assert len(requests) == 2
        assert [event for event, _message, _fields in events] == [
            LogEvent.LLM_CALL,
            LogEvent.LLM_FAILED,
            LogEvent.LLM_CALL,
            LogEvent.LLM_FAILED,
        ]
        assert all("private body" not in repr(event) for event in events)
        assert [fields["err"] for _event, _message, fields in events[1::2]] == [
            "http_status_503", "http_status_503"
        ]
    finally:
        client.close()


def test_tiny_cached_limits_do_not_block_a_send_or_log_a_failure(monkeypatch):
    requests: list[httpx.Request] = []
    client = _client(
        lambda request: requests.append(request) or _response(),
        metadata=ModelMetadata(context_tokens=64, output_tokens=32),
    )
    events = _capture_events(monkeypatch)
    try:
        client.chat([{"role": "user", "content": "word " * 4000}])
        assert len(requests) == 1
        assert LogEvent.LLM_FAILED not in [event for event, _message, _fields in events]
    finally:
        client.close()


def test_previews_never_emit_lifecycle_events_or_send(monkeypatch):
    requests: list[httpx.Request] = []
    client = _client(
        lambda request: requests.append(request) or _response(),
        metadata=ModelMetadata(context_tokens=64, output_tokens=32),
    )
    events = _capture_events(monkeypatch)
    try:
        preview = client.preview_request([{"role": "user", "content": "small"}])
        assert preview.canonical_model_id.endswith("lifecycle-test-model")
        with pytest.raises(LLMPermanentError, match="local budget"):
            client.preview_request([{"role": "user", "content": "word " * 4000}])
        assert requests == []
        assert events == []
    finally:
        client.close()


def test_unexpected_exception_keeps_type_without_failure_event_or_leak(monkeypatch):
    client = _client(lambda _request: pytest.fail("transport must not be called"))
    events = _capture_events(monkeypatch)
    secret_marker = "unexpected-secret-marker"

    def raise_unexpected(*_args, **_kwargs):
        raise RuntimeError(secret_marker)

    monkeypatch.setattr(client, "_invoke_provider", raise_unexpected)
    try:
        with pytest.raises(RuntimeError, match=secret_marker):
            client.chat([{"role": "user", "content": "request"}])
        assert [event for event, _message, _fields in events] == [LogEvent.LLM_CALL]
        assert secret_marker not in repr(events)
    finally:
        client.close()

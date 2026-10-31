"""Focused tests for pinned request transport and attempt-counting gates."""

from __future__ import annotations

import json

import httpx
import pytest

from call_attempt_state import CallAttemptState
from config_schema import AgentConfig, ResolvedProviderConnection
from interfaces import ChatResponse
from llm_client import LLMClient
from model_registry import ModelMetadata, ModelRecord, RunModel


def _client(
    adapter: str = "openai",
    *,
    base_url: str | None = None,
    transport: httpx.BaseTransport | None = None,
    metadata: ModelMetadata | None = None,
) -> LLMClient:
    model = ModelRecord(
        "connection/upstream/model",
        "connection",
        "upstream/model",
        "Pinned display",
        metadata or ModelMetadata(context_tokens=8192, output_tokens=1024),
    )
    connection = ResolvedProviderConnection(
        provider_id="connection",
        adapter=adapter,
        base_url=base_url or f"https://{adapter}.example.test",
        request_timeout=9,
        api_key="fake-key",
    )
    return LLMClient(
        RunModel(model, connection, catalog_revision=5, selection_revision=3),
        agent_config=AgentConfig(ctx_max_tokens=4096),
        http_transport=transport,
    )


def _openai_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": "ok"}}], "usage": {}},
    )


@pytest.mark.parametrize(
    ("adapter", "base_url", "expected_path", "auth_header"),
    [
        ("openai", "https://gw.test/proxy/v1", "/proxy/v1/chat/completions", "authorization"),
        ("openrouter", "https://gw.test/proxy", "/proxy/api/v1/chat/completions", "authorization"),
        ("anthropic", "https://gw.test/proxy", "/proxy/v1/messages", "x-api-key"),
        (
            "google",
            "https://gw.test/proxy/v1beta",
            "/proxy/v1beta/models/upstream%2Fmodel:generateContent",
            "x-goog-api-key",
        ),
    ],
)
def test_provider_request_uses_pinned_route_auth_and_one_count(
    adapter: str, base_url: str, expected_path: str, auth_header: str
) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if adapter == "anthropic":
            return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})
        if adapter == "google":
            return httpx.Response(
                200,
                json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]},
            )
        return _openai_response()

    client = _client(adapter, base_url=base_url, transport=httpx.MockTransport(handler))
    attempts = CallAttemptState(max_retries=2, retry_delay=0)

    with client.bind_attempt(attempts):
        assert client.chat([{"role": "user", "content": "hello"}]) == "ok"

    request = captured[0]
    assert len(captured) == 1
    if adapter == "google":
        assert request.url.raw_path.decode() == expected_path
    else:
        assert request.url.path == expected_path
    assert request.headers.get(auth_header) in {"Bearer fake-key", "fake-key"}
    assert request.extensions["timeout"]["read"] == 9
    assert attempts.outbound_attempts == 1
    assert json.loads(request.content)["messages" if adapter != "google" else "contents"]
    client.close()


def test_native_tool_success_counts_once_and_preserves_nested_definition_fields() -> None:
    captured: list[httpx.Request] = []
    transport = httpx.MockTransport(
        lambda request: (
            captured.append(request)
            or httpx.Response(
                200,
                json={"choices": [{"message": {"tool_calls": [
                    {"id": "call-1", "function": {"name": "lookup", "arguments": "{}"}}
                ]}}]},
            )
        )
    )
    client = _client(transport=transport)
    state = CallAttemptState(max_retries=1, retry_delay=0)
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {
        "type": "object", "properties": {"value": {"type": "string", "x-extra": [1, 2]}}
    }}}]

    with client.bind_attempt(state, definition_revision=8):
        response = client.chat_with_tools([{"role": "user", "content": "call"}], tools)

    assert isinstance(response, ChatResponse)
    assert state.outbound_attempts == 1
    assert json.loads(captured[0].content)["tools"] == tools
    client.close()


def test_preview_uses_final_wire_estimate_without_sending_or_counting() -> None:
    requests: list[httpx.Request] = []
    client = _client(transport=httpx.MockTransport(
        lambda request: requests.append(request) or _openai_response()
    ))

    preview = client.preview_request(
        [{"role": "user", "content": "hello"}],
        tools=[{"type": "function", "function": {"name": "go", "parameters": {"type": "object"}}}],
        definition_revision=12,
    )

    assert preview.canonical_model_id == "connection/upstream/model"
    assert preview.definition_revision == 12
    assert preview.input_tokens > 0
    assert preview.hard_input_budget >= preview.input_tokens
    assert requests == []
    client.close()


def test_unknown_vision_and_unsupported_native_are_rejected_before_send() -> None:
    requests: list[httpx.Request] = []
    client = _client(
        "anthropic",
        transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200, json={})),
    )
    state = CallAttemptState(max_retries=2, retry_delay=0)

    with client.bind_attempt(state):
        with pytest.raises(NotImplementedError, match="unsupported"):
            client.chat_with_tools([{"role": "user", "content": "x"}], [])
        with pytest.raises(Exception, match="verified vision"):
            client.chat([{"role": "user", "content": "x", "images": ["fake-image"]}])

    assert requests == []
    assert state.outbound_attempts == 0
    client.close()


def test_required_cap_without_remaining_combined_capacity_fails_before_send() -> None:
    from llm_client import LLMContextOverflowError, LLMPermanentError

    requests: list[httpx.Request] = []
    client = _client(
        "anthropic",
        transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200, json={})),
        metadata=ModelMetadata(context_tokens=200, output_tokens=50),
    )
    state = CallAttemptState(max_retries=2, retry_delay=0)

    with client.bind_attempt(state):
        with pytest.raises((LLMContextOverflowError, LLMPermanentError), match="local budget"):
            client.chat([{"role": "user", "content": "x " * 2_000}])

    assert requests == []
    assert state.outbound_attempts == 0
    client.close()


def test_http_failure_is_single_count_and_redacts_request_details() -> None:
    client = _client(transport=httpx.MockTransport(lambda request: httpx.Response(503, text="body-secret")))
    state = CallAttemptState(max_retries=1, retry_delay=0)

    with client.bind_attempt(state), pytest.raises(httpx.HTTPStatusError) as caught:
        client.chat([{"role": "user", "content": "request-secret"}])

    assert state.outbound_attempts == 1
    assert "body-secret" not in str(caught.value)
    assert "request-secret" not in str(caught.value)
    assert "fake-key" not in str(caught.value)
    client.close()


def _pinned_client(
    adapter: str,
    base_url: str,
    *,
    api_key: str,
    transport: httpx.BaseTransport,
    metadata: ModelMetadata | None = None,
) -> LLMClient:
    """Build a fresh pinned client through the public RunModel constructor."""
    model = ModelRecord(
        "connection/upstream/model",
        "connection",
        "upstream/model",
        "Pinned display",
        metadata or ModelMetadata(context_tokens=8192, output_tokens=1024),
    )
    connection = ResolvedProviderConnection(
        provider_id="connection",
        adapter=adapter,
        base_url=base_url,
        request_timeout=9,
        api_key=api_key,
    )
    return LLMClient(
        RunModel(model, connection, catalog_revision=5, selection_revision=3),
        agent_config=AgentConfig(ctx_max_tokens=4096),
        http_transport=transport,
    )


def _adapter_response(adapter: str) -> httpx.Response:
    """Return the minimal valid text response shape for one HTTP adapter."""
    if adapter == "anthropic":
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})
    if adapter == "google":
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]},
        )
    return _openai_response()


def _send_text_once(
    adapter: str, base_url: str, *, api_key: str = "fake-key"
) -> httpx.Request:
    """Send one bound text chat on a fresh client and return the sole request."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _adapter_response(adapter)

    client = _pinned_client(
        adapter, base_url, api_key=api_key, transport=httpx.MockTransport(handler)
    )
    try:
        state = CallAttemptState(max_retries=1, retry_delay=0)
        with client.bind_attempt(state):
            assert client.chat([{"role": "user", "content": "hello"}]) == "ok"
        assert state.outbound_attempts == 1
    finally:
        client.close()
    assert len(captured) == 1
    return captured[0]


def _assert_exact_auth(request: httpx.Request, adapter: str) -> None:
    """Assert the adapter's credential header carries exactly the fake key."""
    if adapter in {"openai", "openrouter"}:
        assert request.headers["authorization"] == "Bearer fake-key"
    elif adapter == "anthropic":
        assert request.headers["x-api-key"] == "fake-key"
        assert "authorization" not in request.headers
    else:
        assert request.headers["x-goog-api-key"] == "fake-key"
        assert "authorization" not in request.headers


@pytest.mark.parametrize(
    ("adapter", "base_url", "expected_path"),
    [
        ("openai", "https://gw.test/proxy/v1", "/proxy/v1/chat/completions"),
        ("openrouter", "https://gw.test/proxy/api/v1", "/proxy/api/v1/chat/completions"),
    ],
)
def test_keyless_openai_compatible_request_omits_authorization_header(
    adapter: str, base_url: str, expected_path: str
) -> None:
    request = _send_text_once(adapter, base_url, api_key="")

    assert "authorization" not in request.headers
    assert f"{request.url.scheme}://{request.url.host}" == "https://gw.test"
    assert request.url.path == expected_path
    assert request.url.query == b""
    assert request.extensions["timeout"]["read"] == 9
    assert json.loads(request.content)["model"] == "upstream/model"


@pytest.mark.parametrize(
    ("adapter", "base_url", "expected_raw_path", "expected_model"),
    [
        (
            "openai",
            "https://gw.test/proxy/v1?tenant=a",
            b"/proxy/v1/chat/completions",
            "upstream/model",
        ),
        (
            "openrouter",
            "https://gw.test/proxy/api/v1?tenant=a",
            b"/proxy/api/v1/chat/completions",
            "upstream/model",
        ),
        (
            "anthropic",
            "https://gw.test/proxy/v1?tenant=a",
            b"/proxy/v1/messages",
            "upstream/model",
        ),
        (
            "google",
            "https://gw.test/proxy/v1beta?tenant=a",
            b"/proxy/v1beta/models/upstream%2Fmodel:generateContent",
            None,
        ),
    ],
)
def test_query_bearing_base_url_appends_api_path_to_url_path(
    adapter: str, base_url: str, expected_raw_path: bytes, expected_model: str | None
) -> None:
    request = _send_text_once(adapter, base_url)

    raw_path, _, raw_query = request.url.raw_path.partition(b"?")
    assert f"{request.url.scheme}://{request.url.host}" == "https://gw.test"
    assert raw_path == expected_raw_path
    assert raw_query == b"tenant=a"
    assert request.url.query == b"tenant=a"
    assert request.url.fragment == ""
    _assert_exact_auth(request, adapter)
    assert request.extensions["timeout"]["read"] == 9
    body = json.loads(request.content)
    if expected_model is None:
        assert "model" not in body
    else:
        assert body["model"] == expected_model


@pytest.mark.parametrize(
    ("base_url", "expected_query"),
    [
        ("https://gw.test/proxy/v1#anchor", b""),
        ("https://gw.test/proxy/v1?tenant=a#anchor", b"tenant=a"),
    ],
)
def test_fragment_bearing_base_url_appends_api_path_to_url_path(
    base_url: str, expected_query: bytes
) -> None:
    request = _send_text_once("openai", base_url)

    assert f"{request.url.scheme}://{request.url.host}" == "https://gw.test"
    assert request.url.path == "/proxy/v1/chat/completions"
    assert request.url.query == expected_query
    assert request.url.fragment == "anchor"
    _assert_exact_auth(request, "openai")
    assert request.extensions["timeout"]["read"] == 9
    assert json.loads(request.content)["model"] == "upstream/model"


def test_complete_api_path_with_query_is_not_duplicated() -> None:
    request = _send_text_once("openai", "https://gw.test/proxy/v1/chat/completions?tenant=a")

    assert f"{request.url.scheme}://{request.url.host}" == "https://gw.test"
    assert request.url.path == "/proxy/v1/chat/completions"
    assert request.url.query == b"tenant=a"
    assert request.url.fragment == ""
    _assert_exact_auth(request, "openai")
    assert request.extensions["timeout"]["read"] == 9
    assert json.loads(request.content)["model"] == "upstream/model"


def test_google_native_query_bearing_base_url_routes_to_openai_path() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _openai_response()

    client = _pinned_client(
        "google",
        "https://gw.test/proxy/v1beta?tenant=a",
        api_key="fake-key",
        transport=httpx.MockTransport(handler),
        metadata=ModelMetadata(context_tokens=8192, output_tokens=1024, tool_calling=True),
    )
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
    try:
        state = CallAttemptState(max_retries=1, retry_delay=0)
        with client.bind_attempt(state):
            response = client.chat_with_tools([{"role": "user", "content": "call"}], tools)
        assert state.outbound_attempts == 1
    finally:
        client.close()

    assert isinstance(response, ChatResponse)
    assert len(captured) == 1
    request = captured[0]
    assert f"{request.url.scheme}://{request.url.host}" == "https://gw.test"
    assert request.url.path == "/proxy/v1beta/openai/chat/completions"
    assert request.url.query == b"tenant=a"
    assert request.url.fragment == ""
    _assert_exact_auth(request, "google")
    assert request.extensions["timeout"]["read"] == 9
    assert json.loads(request.content)["model"] == "upstream/model"


def test_query_value_terminal_slash_is_preserved() -> None:
    request = _send_text_once("openai", "https://gw.test/proxy/v1?sig=a/")

    assert f"{request.url.scheme}://{request.url.host}" == "https://gw.test"
    assert request.url.path == "/proxy/v1/chat/completions"
    assert request.url.query == b"sig=a/"
    assert request.headers["authorization"] == "Bearer fake-key"
    assert request.extensions["timeout"]["read"] == 9
    assert json.loads(request.content)["model"] == "upstream/model"


def test_fragment_value_terminal_slash_is_preserved() -> None:
    request = _send_text_once("openai", "https://gw.test/proxy/v1#frg/")

    assert f"{request.url.scheme}://{request.url.host}" == "https://gw.test"
    assert request.url.path == "/proxy/v1/chat/completions"
    assert request.url.query == b""
    assert request.url.fragment == "frg/"
    assert request.headers["authorization"] == "Bearer fake-key"
    assert request.extensions["timeout"]["read"] == 9
    assert json.loads(request.content)["model"] == "upstream/model"

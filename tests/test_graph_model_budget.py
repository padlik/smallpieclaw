"""Graph extraction's fixed wire policy through real pinned clients.

HTTP transport is mocked; routing, usage and provider rejections use
production implementations. No local metadata-limit or input-fit gate exists.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

import httpx
import pytest

from config_schema import AgentConfig, ResolvedProviderConnection
from llm_client import LLMClient
from model_discovery import ModelDiscovery
from model_registry import ModelMetadata, ModelRecord, RunModel
from providers._errors import LLMPermanentError
from token_usage import TokenUsageRegistry

_CAP = 1024
_KEY = "fake-graph-key"
_REPLY = '{"entities":[],"relationships":[]}'
_LONG_TEXT = "graph memory extraction " * 20_000
_TOOLS = [{"type": "function", "function": {"name": "go", "parameters": {"type": "object"}}}]


def _pin(
    metadata: ModelMetadata,
    *,
    adapter: str = "openai",
    upstream: str = "graph-model",
    base_url: str = "",
) -> RunModel:
    """Return a real settled pin for one adapter with fake credentials."""
    record = ModelRecord(f"acct/{upstream}", "acct", upstream, "Graph", metadata)
    connection = ResolvedProviderConnection("acct", adapter, base_url, 15, _KEY)
    return RunModel(record, connection, 1, 1)


def _config(local: int = 4096) -> AgentConfig:
    """Return an agent config with the given local context and no retries."""
    return AgentConfig(ctx_max_tokens=local, max_retries=0)


class _CountingTransport(httpx.MockTransport):
    """Real MockTransport that records every request reaching the wire."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []

        def record(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)

        super().__init__(record)


def _reply_body(adapter: str, mode: str) -> dict[str, Any]:
    """Return a valid adapter-shaped reply carrying 7 prompt / 3 completion tokens."""
    if adapter == "anthropic":
        return {
            "content": [{"type": "text", "text": _REPLY}],
            "usage": {"input_tokens": 7, "output_tokens": 3},
        }
    if adapter == "google" and mode == "json":
        return {
            "candidates": [{"content": {"parts": [{"text": _REPLY}]}}],
            "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 3},
        }
    if adapter == "ollama":
        return {
            "model": "graph-model",
            "created_at": "2026-10-26T00:00:00Z",
            "message": {"role": "assistant", "content": _REPLY},
            "done": True,
            "prompt_eval_count": 7,
            "eval_count": 3,
        }
    return {
        "choices": [{"message": {"content": _REPLY}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3},
    }


def _dig(payload: dict[str, Any], path: tuple[str, ...]) -> object:
    """Return a nested payload value by key path."""
    value: object = payload
    for key in path:
        assert isinstance(value, dict)
        value = value[key]
    return value


_WIRE_CASES = [
    pytest.param("openai", "graph-model", "", "json",
                 "https://api.openai.com/v1/chat/completions",
                 ("max_completion_tokens",), ("temperature",), id="openai"),
    pytest.param("openai", "o3-mini", "", "json",
                 "https://api.openai.com/v1/chat/completions",
                 ("max_completion_tokens",), None, id="openai_reasoning_strips_temperature"),
    pytest.param("openrouter", "vendor/graph-model", "", "json",
                 "https://openrouter.ai/api/v1/chat/completions",
                 ("max_completion_tokens",), ("temperature",), id="openrouter"),
    pytest.param("anthropic", "claude-graph", "", "json",
                 "https://api.anthropic.com/v1/messages",
                 ("max_tokens",), ("temperature",), id="anthropic"),
    pytest.param("google", "gemini-graph", "", "json",
                 "https://generativelanguage.googleapis.com/v1beta/models/gemini-graph:generateContent",
                 ("generationConfig", "maxOutputTokens"), ("generationConfig", "temperature"),
                 id="google_json"),
    pytest.param("google", "gemini-graph", "", "native",
                 "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                 ("max_tokens",), ("temperature",), id="google_native"),
    pytest.param("ollama", "graph-model", "https://ollama.com", "json",
                 "https://ollama.com/api/chat",
                 ("options", "num_predict"), ("options", "temperature"), id="ollama_cloud"),
]


@pytest.mark.parametrize(
    ("adapter", "upstream", "base_url", "mode", "url", "cap_path", "temperature_path"),
    _WIRE_CASES,
)
def test_graph_client_sends_fixed_policy_on_each_adapter_and_counts_usage_once(
    adapter: str,
    upstream: str,
    base_url: str,
    mode: str,
    url: str,
    cap_path: tuple[str, ...],
    temperature_path: tuple[str, ...] | None,
) -> None:
    """One pinned send carries the adapter cap 1024 and temperature 0.1, then counts usage."""
    metadata = ModelMetadata(context_tokens=8192)
    pin = _pin(metadata, adapter=adapter, upstream=upstream, base_url=base_url)
    transport = _CountingTransport(lambda request: httpx.Response(200, json=_reply_body(adapter, mode)))
    registry = TokenUsageRegistry()
    messages = [{"role": "user", "content": "extract"}]
    client = LLMClient.for_graph_extraction(
        pin, agent_config=_config(), http_transport=transport, usage_registry=registry
    )
    try:
        if mode == "native":
            assert client.chat_with_tools(messages, _TOOLS).text == _REPLY
        else:
            assert client.chat(messages, json_mode=True) == _REPLY
        usage = client.get_today_usage()
        assert client.metadata.output_tokens is None
    finally:
        client.close()

    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert (request.method, str(request.url)) == ("POST", url)
    payload = json.loads(request.content)
    assert payload.get("model") == (None if (adapter, mode) == ("google", "json") else upstream)
    assert _dig(payload, cap_path) == _CAP
    if temperature_path is None:
        assert "temperature" not in payload
    else:
        assert _dig(payload, temperature_path) == 0.1
    assert (usage["prompt_tokens"], usage["completion_tokens"], usage["total_tokens"]) == (7, 3, 10)
    assert registry.get_today() == {pin.model.id: {"prompt": 7, "completion": 3, "total": 10}}
    assert pin == _pin(metadata, adapter=adapter, upstream=upstream, base_url=base_url)


def _assert_single_rejected_send(
    pin: RunModel, transport: _CountingTransport, url: str, cap_path: tuple[str, ...]
) -> None:
    """Run one graph chat that the provider rejects and assert no retry or accounting."""
    registry = TokenUsageRegistry()
    client = LLMClient.for_graph_extraction(
        pin, agent_config=_config(), http_transport=transport, usage_registry=registry
    )
    try:
        with pytest.raises(LLMPermanentError) as caught:
            client.chat([{"role": "user", "content": "extract"}], json_mode=True)
        usage = client.get_today_usage()
        assert client.metadata.output_tokens is None
    finally:
        client.close()
    assert type(caught.value) is LLMPermanentError
    assert _KEY not in str(caught.value)
    assert len(transport.requests) == 1
    assert str(transport.requests[0].url) == url
    assert _dig(json.loads(transport.requests[0].content), cap_path) == _CAP
    assert usage["total_tokens"] == 0
    assert registry.get_today() == {}


def test_provider_rejecting_fixed_cap_with_unknown_output_fails_after_one_send() -> None:
    """A 400 on the fixed cap is permanent: one send, no lowered cap, no accounting."""
    transport = _CountingTransport(lambda request: httpx.Response(400, json={"error": {
        "type": "invalid_request_error",
        "message": "Unsupported parameter: 'max_completion_tokens'",
    }}))
    _assert_single_rejected_send(
        _pin(ModelMetadata(context_tokens=8192)), transport,
        "https://api.openai.com/v1/chat/completions", ("max_completion_tokens",),
    )


def test_discovered_google_methods_infer_no_limits_and_rejection_stays_single_send() -> None:
    """Google resource names and generation methods imply no token limits or caps."""
    row = {
        "name": "models/gemini-graph-resource-001",
        "baseModelId": "gemini-graph",
        "supportedGenerationMethods": ["generateContent", "countTokens"],
    }
    provider = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"models": [row]})
    ))
    public = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={})
    ))
    connection = ResolvedProviderConnection("acct", "google", "", 15, _KEY)
    try:
        inventory = ModelDiscovery(
            provider, lambda: datetime(2026, 10, 26, tzinfo=timezone.utc)
        ).fetch(connection)
    finally:
        provider.close()
        public.close()

    record = inventory.models[0]
    assert (record.id, record.upstream_id) == ("acct/gemini-graph", "gemini-graph")
    metadata = record.metadata
    assert (metadata.context_tokens, metadata.input_tokens, metadata.output_tokens) == (
        None, None, None
    )

    transport = _CountingTransport(lambda request: httpx.Response(400, json={"error": {
        "code": 400,
        "status": "INVALID_ARGUMENT",
        "message": "Invalid value at 'generation_config.max_output_tokens'",
    }}))
    _assert_single_rejected_send(
        RunModel(record, connection, 1, 1), transport,
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-graph:generateContent",
        ("generationConfig", "maxOutputTokens"),
    )


def test_ordinary_required_cap_comes_from_config_while_graph_keeps_its_fixed_cap() -> None:
    """Ordinary Anthropic sends the configured cap without discovered output; the graph cap is separate."""
    metadata = ModelMetadata(context_tokens=200_000)
    pin = _pin(metadata, adapter="anthropic", upstream="claude-graph")
    ordinary_transport = _CountingTransport(
        lambda request: httpx.Response(200, json=_reply_body("anthropic", "json"))
    )
    ordinary = LLMClient(
        pin, agent_config=AgentConfig(ctx_max_tokens=4096, max_retries=0, required_output_cap_tokens=2000),
        http_transport=ordinary_transport,
    )
    try:
        assert ordinary.chat([{"role": "user", "content": "hello"}]) == _REPLY
    finally:
        ordinary.close()
    assert len(ordinary_transport.requests) == 1
    assert json.loads(ordinary_transport.requests[0].content)["max_tokens"] == 2000

    graph_transport = _CountingTransport(
        lambda request: httpx.Response(200, json=_reply_body("anthropic", "json"))
    )
    graph = LLMClient.for_graph_extraction(pin, agent_config=_config(), http_transport=graph_transport)
    try:
        assert graph.chat([{"role": "user", "content": "extract"}]) == _REPLY
    finally:
        graph.close()
    assert len(graph_transport.requests) == 1
    assert json.loads(graph_transport.requests[0].content)["max_tokens"] == _CAP

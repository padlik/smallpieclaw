"""Graph extraction budget: fixed 1024-token policy over the pinned model limits.

These tests characterize the public budget kernel and real extraction client
using supplied settled pins. HTTP transport is mocked; budgets, estimation,
routing and usage accounting use production implementations. Startup,
backfill and admission wiring are not exercised here.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from typing import Any, Callable

import httpx
import pytest

from config_schema import AgentConfig, ResolvedProviderConnection
from llm_client import LLMClient
from model_discovery import ModelDiscovery
from model_registry import ModelMetadata, ModelRecord, RunModel
from model_request_budget import (
    ModelRequestBudget,
    ModelRequestBudgetError,
    compute_request_budget,
    repair_input_ceiling,
)
from providers._errors import LLMContextOverflowError, LLMPermanentError
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


# (metadata, local context, combined, input limit, hard budget, assumed context)
_GRAPH_MATRIX = [
    pytest.param(ModelMetadata(context_tokens=8192), 4096, 8192, None, 7168, False,
                 id="context_only_unknown_output"),
    pytest.param(ModelMetadata(context_tokens=10_000, input_tokens=8000), 4096,
                 10_000, 8000, 8000, False, id="input_limit_below_context"),
    pytest.param(ModelMetadata(context_tokens=10_000, input_tokens=9000), 4096,
                 10_000, 9000, 8976, False, id="context_minus_cap_below_input_limit"),
    pytest.param(ModelMetadata(input_tokens=8000), 1, None, 8000, 8000, False,
                 id="input_only_ignores_tiny_local"),
    pytest.param(ModelMetadata(), 12_000, 12_000, None, 10_976, True,
                 id="no_limits_uses_local_context"),
    pytest.param(ModelMetadata(context_tokens=8192, output_tokens=64_000), 4096,
                 8192, None, 7168, False, id="large_known_output_keeps_fixed_reserve"),
    pytest.param(ModelMetadata(context_tokens=1025), 4096, 1025, None, 1, False,
                 id="one_token_headroom"),
]


@pytest.mark.parametrize(
    ("metadata", "local", "combined", "input_limit", "budget", "assumed"), _GRAPH_MATRIX
)
def test_kernel_fixed_cap_report_and_exact_fit_boundary(
    metadata: ModelMetadata,
    local: int,
    combined: int | None,
    input_limit: int | None,
    budget: int,
    assumed: bool,
) -> None:
    """The fixed cap always reserves 1024, fits I == B and rejects I == B + 1."""
    report = compute_request_budget(
        metadata, input_tokens=0, local_context_tokens=local, fixed_output_cap=_CAP
    )
    assert report == ModelRequestBudget(
        input_tokens=0,
        hard_input_budget=budget,
        output_cap=_CAP,
        completion_reserve=_CAP,
        combined_context=combined,
        input_limit=input_limit,
        assumed_context=assumed,
        assumed_completion_reserve=False,
    )

    at_limit = compute_request_budget(
        metadata, input_tokens=budget, local_context_tokens=local, fixed_output_cap=_CAP
    )
    assert (at_limit.input_tokens, at_limit.hard_input_budget) == (budget, budget)
    assert at_limit.output_cap == _CAP

    with pytest.raises(ModelRequestBudgetError) as caught:
        compute_request_budget(
            metadata, input_tokens=budget + 1, local_context_tokens=local,
            fixed_output_cap=_CAP,
        )
    assert caught.value.code == "input_exceeds_budget"
    assert repair_input_ceiling(
        metadata, local_context_tokens=local, fixed_output_cap=_CAP
    ) == budget


@pytest.mark.parametrize(
    ("metadata", "local", "code"),
    [
        pytest.param(ModelMetadata(context_tokens=8192, output_tokens=1023), 4096,
                     "output_limit_too_small", id="known_output_1023"),
        pytest.param(ModelMetadata(context_tokens=1024), 4096, "no_input_budget",
                     id="context_1024"),
        pytest.param(ModelMetadata(context_tokens=1023), 4096, "no_input_budget",
                     id="context_1023"),
        pytest.param(ModelMetadata(), 1024, "no_input_budget", id="local_1024_no_limits"),
    ],
)
def test_kernel_rejects_prerequisites_at_zero_input(
    metadata: ModelMetadata, local: int, code: str
) -> None:
    """Too-small output limits and zero headroom fail typed even for empty input."""
    with pytest.raises(ModelRequestBudgetError) as caught:
        compute_request_budget(
            metadata, input_tokens=0, local_context_tokens=local, fixed_output_cap=_CAP
        )
    assert caught.value.code == code
    with pytest.raises(ModelRequestBudgetError) as repair:
        repair_input_ceiling(metadata, local_context_tokens=local, fixed_output_cap=_CAP)
    assert repair.value.code == code


@pytest.mark.parametrize("output_tokens", [1024, None], ids=["known_1024", "unknown"])
def test_kernel_accepts_output_threshold_and_unknown_output(output_tokens: int | None) -> None:
    """A known 1024 output limit or an unknown one admits the fixed cap unchanged."""
    report = compute_request_budget(
        ModelMetadata(context_tokens=8192, output_tokens=output_tokens),
        input_tokens=0,
        local_context_tokens=4096,
        fixed_output_cap=_CAP,
    )
    assert (report.output_cap, report.completion_reserve) == (_CAP, _CAP)
    assert report.hard_input_budget == 7168
    assert report.assumed_completion_reserve is False


_INVALID_LIMITS = [
    0, -1, True, "8192", 1.5,
    pytest.param(float("inf"), id="inf"),
    pytest.param(float("nan"), id="nan"),
]


@pytest.mark.parametrize("value", _INVALID_LIMITS)
@pytest.mark.parametrize("field", ["context_tokens", "input_tokens", "output_tokens"])
def test_kernel_rejects_invalid_raw_metadata_limits(field: str, value: object) -> None:
    """Non-positive, boolean, string and non-integer catalog limits are invalid."""
    metadata = ModelMetadata(**{field: value})
    with pytest.raises(ModelRequestBudgetError) as caught:
        compute_request_budget(
            metadata, input_tokens=0, local_context_tokens=4096, fixed_output_cap=_CAP
        )
    assert caught.value.code == "invalid_limit"
    with pytest.raises(ModelRequestBudgetError) as repair:
        repair_input_ceiling(metadata, local_context_tokens=4096, fixed_output_cap=_CAP)
    assert repair.value.code == "invalid_limit"


@pytest.mark.parametrize(
    ("input_tokens", "local"),
    [(-1, 4096), (True, 4096), (1.5, 4096), (0, 0), (0, -5), (0, True)],
    ids=["input_negative", "input_bool", "input_float", "local_zero", "local_negative", "local_bool"],
)
def test_kernel_rejects_invalid_input_and_local_context(input_tokens: object, local: object) -> None:
    """Input must be a non-negative int and local context a positive int."""
    with pytest.raises(ModelRequestBudgetError) as caught:
        compute_request_budget(
            ModelMetadata(context_tokens=8192),
            input_tokens=input_tokens,
            local_context_tokens=local,
            fixed_output_cap=_CAP,
        )
    assert caught.value.code == "invalid_limit"


@pytest.mark.parametrize("output_tokens", [1023, 1024, None], ids=["1023", "1024", "unknown"])
def test_factory_checks_known_output_threshold_before_any_io(output_tokens: int | None) -> None:
    """The factory rejects only a known output limit below 1024, without sending."""
    transport = _CountingTransport(lambda request: httpx.Response(500))
    pin = _pin(ModelMetadata(context_tokens=8192, output_tokens=output_tokens))
    if output_tokens == 1023:
        with pytest.raises(LLMPermanentError) as caught:
            LLMClient.for_graph_extraction(pin, agent_config=_config(), http_transport=transport)
        assert type(caught.value) is LLMPermanentError
    else:
        client = LLMClient.for_graph_extraction(pin, agent_config=_config(), http_transport=transport)
        client.close()
    assert transport.requests == []


@pytest.mark.parametrize(
    ("metadata", "local", "ceiling"),
    [
        pytest.param(ModelMetadata(context_tokens=8192), 4096, 7168, id="context_only"),
        pytest.param(ModelMetadata(input_tokens=8000), 1, 8000, id="input_only"),
        pytest.param(ModelMetadata(context_tokens=10_000, input_tokens=9000), 4096, 8976,
                     id="context_and_input"),
        pytest.param(ModelMetadata(), 12_000, 10_976, id="no_limits_local"),
    ],
)
def test_graph_client_prefit_overflow_raises_before_any_send(
    metadata: ModelMetadata, local: int, ceiling: int
) -> None:
    """Oversized real messages fail with a repair target and leave no side effects."""
    pin = _pin(metadata)
    transport = _CountingTransport(lambda request: httpx.Response(200, json=_reply_body("openai", "json")))
    registry = TokenUsageRegistry()
    messages = [{"role": "user", "content": _LONG_TEXT}]
    snapshot = copy.deepcopy(messages)
    client = LLMClient.for_graph_extraction(
        pin, agent_config=_config(local), http_transport=transport, usage_registry=registry
    )
    try:
        before = client.get_today_usage()
        with pytest.raises(LLMContextOverflowError) as caught:
            client.chat(messages, json_mode=True)
        after = client.get_today_usage()
    finally:
        client.close()

    assert type(caught.value) is LLMContextOverflowError
    assert caught.value.status_code is None
    assert caught.value.repair_input_ceiling == ceiling
    assert ceiling == repair_input_ceiling(
        metadata, local_context_tokens=local, fixed_output_cap=_CAP
    )
    assert caught.value.input_tokens is not None
    assert caught.value.input_tokens > ceiling
    assert transport.requests == []
    assert before == after
    assert after["total_tokens"] == 0
    assert registry.get_today() == {}
    assert messages == snapshot
    assert pin == _pin(metadata)


@pytest.mark.parametrize(
    ("metadata", "local"),
    [
        pytest.param(ModelMetadata(context_tokens=1024), 4096, id="context_1024"),
        pytest.param(ModelMetadata(), 1024, id="local_1024_no_limits"),
    ],
)
def test_graph_client_without_headroom_fails_permanently_before_io(
    metadata: ModelMetadata, local: int
) -> None:
    """No positive input headroom is a permanent local failure, not an overflow."""
    transport = _CountingTransport(lambda request: httpx.Response(500))
    registry = TokenUsageRegistry()
    client = LLMClient.for_graph_extraction(
        _pin(metadata), agent_config=_config(local), http_transport=transport,
        usage_registry=registry,
    )
    try:
        with pytest.raises(LLMPermanentError) as caught:
            client.chat([{"role": "user", "content": "extract"}], json_mode=True)
        usage = client.get_today_usage()
    finally:
        client.close()
    assert type(caught.value) is LLMPermanentError
    assert "no_input_budget" in str(caught.value)
    assert transport.requests == []
    assert usage["total_tokens"] == 0
    assert registry.get_today() == {}


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
            provider, public, lambda: datetime(2026, 10, 26, tzinfo=timezone.utc)
        ).fetch(connection)
    finally:
        provider.close()
        public.close()

    record = inventory.models[0]
    assert (record.id, record.upstream_id) == ("acct/gemini-graph", "gemini-graph")
    assert inventory.descriptors[0].resource_name == "models/gemini-graph-resource-001"
    assert inventory.descriptors[0].generation_methods == ("generateContent", "countTokens")
    metadata = record.metadata
    assert (metadata.context_tokens, metadata.input_tokens, metadata.output_tokens) == (
        None, None, None
    )
    assert not {"context_tokens", "input_tokens", "output_tokens"} & set(dict(metadata.sources))

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


def test_ordinary_required_cap_still_needs_known_output_while_graph_does_not() -> None:
    """The graph exception never lets ordinary Anthropic requests send without a cap."""
    metadata = ModelMetadata(context_tokens=200_000)
    with pytest.raises(ModelRequestBudgetError) as caught:
        compute_request_budget(
            metadata, input_tokens=0, local_context_tokens=4096, output_cap_required=True
        )
    assert caught.value.code == "output_cap_unavailable"
    assert compute_request_budget(
        metadata, input_tokens=0, local_context_tokens=4096, fixed_output_cap=_CAP
    ).output_cap == _CAP

    pin = _pin(metadata, adapter="anthropic", upstream="claude-graph")
    ordinary_transport = _CountingTransport(
        lambda request: httpx.Response(200, json=_reply_body("anthropic", "json"))
    )
    ordinary = LLMClient(pin, agent_config=_config(), http_transport=ordinary_transport)
    try:
        with pytest.raises(LLMPermanentError) as rejected:
            ordinary.chat([{"role": "user", "content": "hello"}])
        ordinary_usage = ordinary.get_today_usage()
    finally:
        ordinary.close()
    assert type(rejected.value) is LLMPermanentError
    assert "output_cap_unavailable" in str(rejected.value)
    assert ordinary_transport.requests == []
    assert ordinary_usage["total_tokens"] == 0

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

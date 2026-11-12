"""Task 5.4: graph extraction keeps its fixed wire policy with no fit gate.

Covers ``per-model-context-window`` / "Graph extraction keeps its fixed wire
policy without a fit gate" and ``provider-model-catalog`` / "Graph extraction
resolves its model independently of main selection" (ADR-0042).  Real
``LLMClient`` objects send through ``httpx.MockTransport`` with canary keys.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import httpx
import pytest

from config_schema import AgentConfig, ResolvedProviderConnection, parse_config
from graph_memory import GraphPrerequisites, build_extraction_llm_call, resolve_graph_startup
from llm_client import LLMClient
from model_registry import ModelMetadata, ModelRecord, RunModel
from providers._errors import LLMContextOverflowError

_KEY = "graph-canary-key-54"
_HUGE = "graph memory extraction " * 20_000


class _Transport(httpx.MockTransport):
    """Records requests and close calls."""

    def __init__(self, handler: Any) -> None:
        super().__init__(handler)
        self.requests: list[httpx.Request] = []
        self.closed = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return super().handle_request(request)

    def close(self) -> None:
        self.closed += 1


def _reply(adapter: str) -> dict[str, Any]:
    text = '{"entities":[],"relationships":[]}'
    if adapter == "anthropic":
        return {"content": [{"type": "text", "text": text}], "usage": {"input_tokens": 1, "output_tokens": 1}}
    if adapter == "google":
        return {"candidates": [{"content": {"parts": [{"text": text}]}}], "usageMetadata": {}}
    if adapter == "ollama":
        return {"message": {"role": "assistant", "content": text}, "done": True}
    return {"choices": [{"message": {"content": text}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


def _pin(adapter: Any, metadata: ModelMetadata, base_url: str = "") -> RunModel:
    record = ModelRecord("graph-extraction/m", "graph-extraction", "m", "m", metadata)
    connection = ResolvedProviderConnection("graph-extraction", adapter, base_url, 15, _KEY)
    return RunModel(record, connection, 0, 0)


_ADAPTERS = [
    pytest.param("openai", "", ("max_completion_tokens",), ("temperature",), id="openai"),
    pytest.param("anthropic", "", ("max_tokens",), ("temperature",), id="anthropic"),
    pytest.param("google", "", ("generationConfig", "maxOutputTokens"), ("generationConfig", "temperature"), id="google"),
    pytest.param("ollama", "https://ollama.com", ("options", "num_predict"), ("options", "temperature"), id="ollama"),
]


def _dig(payload: dict[str, Any], path: tuple[str, ...]) -> Any:
    for key in path:
        payload = payload[key]
    return payload


@pytest.mark.parametrize("metadata", [
    pytest.param(ModelMetadata(context_tokens=1024, output_tokens=512), id="tiny-known"),
    pytest.param(ModelMetadata(), id="unknown"),
])
@pytest.mark.parametrize(("adapter", "base_url", "cap_path", "temp_path"), _ADAPTERS)
def test_oversized_graph_prompt_is_sent_with_fixed_wire_and_no_local_fit_check(
    adapter: str, base_url: str, cap_path: tuple[str, ...], temp_path: tuple[str, ...], metadata: ModelMetadata
) -> None:
    """A prompt above the ordinary window and tiny limits still goes out with 1024/0.1."""
    transport = _Transport(lambda request: httpx.Response(200, json=_reply(adapter)))
    client = LLMClient.for_graph_extraction(
        _pin(adapter, metadata, base_url),
        agent_config=AgentConfig(ctx_max_tokens=1024, max_retries=0),
        http_transport=transport,
    )
    try:
        client.chat([{"role": "user", "content": _HUGE}], json_mode=True)
    finally:
        client.close()
    assert len(transport.requests) == 1
    payload = json.loads(transport.requests[0].content)
    assert _dig(payload, cap_path) == 1024
    assert _dig(payload, temp_path) == 0.1


@pytest.mark.parametrize("output_tokens", [1023, 1024, None], ids=["1023", "1024", "unknown"])
def test_factory_has_no_output_limit_gate(output_tokens: int | None) -> None:
    """Discovered output limits never reject the graph factory; nothing is sent."""
    transport = _Transport(lambda request: httpx.Response(500))
    client = LLMClient.for_graph_extraction(
        _pin("openai", ModelMetadata(context_tokens=8192, output_tokens=output_tokens)),
        agent_config=AgentConfig(max_retries=0),
        http_transport=transport,
    )
    client.close()
    assert transport.requests == []
    assert transport.closed == 1


def test_provider_rejection_of_oversized_graph_prompt_is_one_send_without_cap_reduction() -> None:
    """The provider decides: one send, fixed cap retained, ordinary failure type."""
    transport = _Transport(lambda request: httpx.Response(400, json={"error": {"message": "context_length_exceeded"}}))
    client = LLMClient.for_graph_extraction(
        _pin("openai", ModelMetadata()),
        agent_config=AgentConfig(ctx_max_tokens=1024, max_retries=0),
        http_transport=transport,
    )
    try:
        with pytest.raises(LLMContextOverflowError):
            client.chat([{"role": "user", "content": _HUGE}], json_mode=True)
    finally:
        client.close()
    assert len(transport.requests) == 1
    assert json.loads(transport.requests[0].content)["max_completion_tokens"] == 1024


def test_ordinary_client_sends_the_same_oversized_prompt_without_a_local_refusal() -> None:
    """Control: task 6.2 removed the ordinary local size refusal; the prompt is sent."""
    transport = _Transport(lambda request: httpx.Response(200, json=_reply("openai")))
    client = LLMClient(
        _pin("openai", ModelMetadata()),
        agent_config=AgentConfig(ctx_max_tokens=4096, max_retries=0),
        http_transport=transport,
    )
    try:
        client.chat([{"role": "user", "content": _HUGE}], json_mode=True)
    finally:
        client.close()
    assert len(transport.requests) == 1


def test_graph_startup_has_no_budget_prerequisite_even_with_tiny_local_context() -> None:
    """ctx_max_tokens below the fixed cap must not make graph startup fail."""
    raw: dict[str, Any] = {
        "telegram": {"bot_token": "123456:ABC-DEF", "allowed_user_ids": [1], "security_mode": "allowlist"},
        "agent": {"compaction_threshold_tokens": 1024},
        "paths": {},
        "providers": {"chat": {"type": "openai", "api_key": "chat-canary", "base_url": "https://chat.x.test/v1"}},
        "embeddings": {"provider": "openai", "base_url": "https://embed.x.test/v1", "api_key": "embed-canary", "model": "e"},
        "graph_memory": {
            "enabled": True,
            "extraction_provider": "openai",
            "extraction_base_url": "https://graph.x.test/v1",
            "extraction_api_key": _KEY,
            "extraction_model": "vendor/m",
        },
    }
    parsed = parse_config(raw)
    # ``agent.ctx_max_tokens`` can no longer be configured; inject the retained internal
    # attribute directly so the tiny-local-context proof is not weakened (removed in 6.2).
    tiny = dataclasses.replace(parsed, agent=dataclasses.replace(parsed.agent, ctx_max_tokens=1024))
    for app_cfg in (parsed, tiny):
        result = resolve_graph_startup(app_cfg)
        assert isinstance(result, GraphPrerequisites)
        assert not hasattr(result, "budget")


def test_two_extractions_use_distinct_owned_clients_on_the_graph_route_only() -> None:
    """Each call builds and closes its own client; only the graph endpoint and key are used."""
    transports: list[_Transport] = []
    real = LLMClient.for_graph_extraction.__func__  # type: ignore[attr-defined]

    def factory(cls: type[LLMClient], *args: Any, **kwargs: Any) -> LLMClient:
        transport = _Transport(lambda request: httpx.Response(200, json=_reply("openai")))
        transports.append(transport)
        return real(cls, *args, http_transport=transport, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(LLMClient, "for_graph_extraction", classmethod(factory))
        call = build_extraction_llm_call(
            _pin("openai", ModelMetadata(), "https://graph.x.test/v1"), AgentConfig(max_retries=0)
        )
        call("one")
        call("two")
    assert len(transports) == 2 and transports[0] is not transports[1]
    assert [t.closed for t in transports] == [1, 1]
    for transport in transports:
        (request,) = transport.requests
        assert request.url.host == "graph.x.test"
        assert request.headers["authorization"] == f"Bearer {_KEY}"


def test_cancellation_during_send_still_closes_the_owned_client() -> None:
    """A BaseException (cancel) from the transport propagates and the client is closed."""
    transports: list[_Transport] = []
    real = LLMClient.for_graph_extraction.__func__  # type: ignore[attr-defined]

    def boom(request: httpx.Request) -> httpx.Response:
        raise KeyboardInterrupt

    def factory(cls: type[LLMClient], *args: Any, **kwargs: Any) -> LLMClient:
        transport = _Transport(boom)
        transports.append(transport)
        return real(cls, *args, http_transport=transport, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(LLMClient, "for_graph_extraction", classmethod(factory))
        call = build_extraction_llm_call(_pin("openai", ModelMetadata()), AgentConfig(max_retries=0))
        with pytest.raises(KeyboardInterrupt):
            call("x")
    assert len(transports) == 1 and len(transports[0].requests) == 1
    assert transports[0].closed == 1

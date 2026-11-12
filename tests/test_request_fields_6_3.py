"""Task 6.3: only mandatory-output adapters get the configured cap on the wire.

Every request here goes through the real ``LLMClient`` and an
``httpx.MockTransport`` (Ollama through the installed SDK), and the assertions
read the complete serialized HTTP body.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from config_schema import AgentConfig, ResolvedProviderConnection
from llm_client import LLMClient
from model_registry import ModelMetadata, ModelRecord, RunModel

_BASE_URLS = {
    "openai": "https://api.example.test/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "google": "https://generativelanguage.googleapis.com",
    "ollama": "https://ollama.com",
    "anthropic": "https://api.example.test",
}
_TOOLS = [
    {"type": "function", "function": {"name": "go", "parameters": {"type": "object"}}}
]
_FORBIDDEN_KEYS = frozenset({
    "max_tokens", "max_completion_tokens", "max_output_tokens", "maxOutputTokens",
    "temperature", "top_p", "topP", "top_k", "topK", "num_predict", "num_ctx",
    "options",
})
_OLLAMA_OK = {
    "model": "model", "created_at": "2026-10-14T00:00:00Z",
    "message": {"role": "assistant", "content": "ok"}, "done": True,
}
_RESPONSES: dict[str, dict[str, Any]] = {
    "openai": {"choices": [{"message": {"content": "ok"}}]},
    "openrouter": {"choices": [{"message": {"content": "ok"}}]},
    "google": {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]},
    "ollama": _OLLAMA_OK,
    "anthropic": {"content": [{"type": "text", "text": "ok"}]},
}


def _run_model(
    adapter: str, *, context: int | None = 4096, output: int | None = 2048
) -> RunModel:
    model = ModelRecord(
        "p/model", "p", "model", "Model",
        ModelMetadata(context_tokens=context, output_tokens=output, tool_calling=True),
    )
    connection = ResolvedProviderConnection(
        "p", adapter, _BASE_URLS[adapter], 15, "fake-key"
    )
    return RunModel(model, connection, 1, 1)


def _keys(payload: dict[str, Any]) -> set[str]:
    """Generation-parameter locations only: top level, generationConfig, options.

    Messages, contents and tool schemas are caller data and may legitimately
    contain property names such as ``temperature``.
    """
    found = set(payload)
    for nested in ("generationConfig", "options"):
        child = payload.get(nested)
        if isinstance(child, dict):
            found |= set(child)
    return found


def _send(
    adapter: str,
    mode: str,
    *,
    run_model: RunModel | None = None,
    config: AgentConfig | None = None,
    prompt: str = "hello",
    graph: bool = False,
    tools: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], httpx.Request]:
    """Send one real call and return the decoded wire body and request."""
    requests: list[httpx.Request] = []
    body = dict(_RESPONSES[adapter])
    if mode == "native" and adapter in {"google"}:
        body = _RESPONSES["openai"]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=body)

    factory = LLMClient.for_graph_extraction if graph else LLMClient
    client = factory(
        run_model or _run_model(adapter),
        agent_config=config or AgentConfig(),
        http_transport=httpx.MockTransport(handler),
    )
    messages = [{"role": "user", "content": prompt}]
    try:
        if mode == "native":
            client.chat_with_tools(messages, tools or _TOOLS)
        else:
            client.chat(messages, json_mode=mode == "json")
    finally:
        client.close()
    assert len(requests) == 1
    return json.loads(requests[0].content), requests[0]


_ORDINARY = ["openai", "openrouter", "google", "ollama"]
_MODES = ["text", "json", "native"]


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("adapter", _ORDINARY)
class TestOrdinaryOmitsOptionalFields:
    def test_no_optional_field_anywhere_in_wire_body(self, adapter: str, mode: str) -> None:
        payload, _ = _send(adapter, mode)
        assert not _FORBIDDEN_KEYS & _keys(payload)

    @pytest.mark.parametrize(
        ("context", "output"), [(1, 1), (10**9, 10**9), (None, None), (4096, 7)]
    )
    def test_discovered_limits_never_add_or_resize_fields(
        self, adapter: str, mode: str, context: int | None, output: int | None
    ) -> None:
        payload, _ = _send(adapter, mode, run_model=_run_model(adapter, context=context, output=output))
        assert not _FORBIDDEN_KEYS & _keys(payload)

    def test_custom_required_cap_does_not_leak_to_ordinary_adapter(
        self, adapter: str, mode: str
    ) -> None:
        config = AgentConfig(required_output_cap_tokens=4321)
        payload, _ = _send(adapter, mode, config=config)
        assert not _FORBIDDEN_KEYS & _keys(payload)
        assert "4321" not in json.dumps(payload)

    def test_prompt_far_over_discovered_window_is_still_sent(
        self, adapter: str, mode: str
    ) -> None:
        payload, _ = _send(
            adapter, mode, run_model=_run_model(adapter, context=8, output=1),
            prompt="word " * 20_000,
        )
        assert not _FORBIDDEN_KEYS & _keys(payload)


class TestOrdinaryWireSemanticsPreserved:
    def test_google_json_keeps_only_response_mime_type(self) -> None:
        payload, request = _send("google", "json")
        assert payload["generationConfig"] == {"responseMimeType": "application/json"}
        assert request.headers["x-goog-api-key"] == "fake-key"

    def test_openai_json_and_native_semantics(self) -> None:
        json_payload, request = _send("openai", "json")
        assert json_payload["response_format"] == {"type": "json_object"}
        assert request.headers["authorization"] == "Bearer fake-key"
        native, _ = _send("openai", "native")
        assert native["tools"] == _TOOLS
        assert native["tool_choice"] == "auto"

    def test_openrouter_origin_and_credentials_unchanged(self) -> None:
        _, request = _send("openrouter", "text")
        assert str(request.url).startswith("https://openrouter.ai/api/v1")
        assert request.headers["authorization"] == "Bearer fake-key"

    def test_ollama_json_and_native_semantics(self) -> None:
        json_payload, request = _send("ollama", "json")
        assert json_payload["format"] == "json"
        assert json_payload["stream"] is False
        assert str(request.url) == "https://ollama.com/api/chat"
        native, _ = _send("ollama", "native")
        assert native["tools"][0]["function"]["name"] == "go"


@pytest.mark.parametrize("mode", ["text", "json"])
class TestAnthropicMandatoryCap:
    def test_default_cap_is_1024(self, mode: str) -> None:
        payload, request = _send("anthropic", mode)
        assert payload["max_tokens"] == 1024
        assert request.headers["x-api-key"] == "fake-key"
        assert _keys(payload) & _FORBIDDEN_KEYS == {"max_tokens"}

    def test_custom_cap_is_sent_exactly(self, mode: str) -> None:
        payload, _ = _send(
            "anthropic", mode, config=AgentConfig(required_output_cap_tokens=4096)
        )
        assert payload["max_tokens"] == 4096

    @pytest.mark.parametrize(("context", "output"), [(1, 1), (10**9, 10**9), (None, None)])
    def test_discovered_limits_cannot_resize_or_drop_cap(
        self, mode: str, context: int | None, output: int | None
    ) -> None:
        payload, _ = _send(
            "anthropic", mode, run_model=_run_model("anthropic", context=context, output=output),
            config=AgentConfig(required_output_cap_tokens=3000),
        )
        assert payload["max_tokens"] == 3000
        assert "temperature" not in payload and "top_p" not in payload

    def test_oversized_prompt_does_not_reserve_or_reject(self, mode: str) -> None:
        payload, _ = _send(
            "anthropic", mode, run_model=_run_model("anthropic", context=8, output=1),
            prompt="word " * 20_000,
        )
        assert payload["max_tokens"] == 1024


class TestGraphFixedWireStaysSeparate:
    @pytest.mark.parametrize("cap", [1024, 4096])
    def test_openai_graph_fixed_regardless_of_ordinary_cap(self, cap: int) -> None:
        config = AgentConfig(required_output_cap_tokens=cap)
        payload, _ = _send("openai", "json", config=config, graph=True)
        assert payload["max_completion_tokens"] == 1024
        assert payload["temperature"] == 0.1

    def test_anthropic_graph_fixed_while_ordinary_uses_configured_cap(self) -> None:
        config = AgentConfig(required_output_cap_tokens=4096)
        graph, _ = _send("anthropic", "text", config=config, graph=True)
        ordinary, _ = _send("anthropic", "text", config=config)
        assert (graph["max_tokens"], graph["temperature"]) == (1024, 0.1)
        assert ordinary["max_tokens"] == 4096
        assert "temperature" not in ordinary

    def test_google_graph_fixed_and_ordinary_unaffected(self) -> None:
        graph, _ = _send("google", "json", graph=True)
        assert graph["generationConfig"]["maxOutputTokens"] == 1024
        assert graph["generationConfig"]["temperature"] == 0.1
        ordinary, _ = _send("google", "json")
        assert ordinary["generationConfig"] == {"responseMimeType": "application/json"}

    def test_ollama_graph_fixed_and_ordinary_unaffected(self) -> None:
        graph, _ = _send("ollama", "text", graph=True)
        assert graph["options"] == {"num_predict": 1024, "temperature": 0.1}
        ordinary, _ = _send("ollama", "text")
        assert "options" not in ordinary


_KNOB_NAMED_TOOL = [{
    "type": "function",
    "function": {
        "name": "tune",
        "parameters": {
            "type": "object",
            "properties": {k: {"type": "number"} for k in ("temperature", "top_p", "max_tokens")},
        },
    },
}]


@pytest.mark.parametrize("adapter", _ORDINARY)
def test_tool_property_names_are_caller_data_not_generation_fields(adapter: str) -> None:
    payload, _ = _send(adapter, "native", tools=_KNOB_NAMED_TOOL)
    assert not _FORBIDDEN_KEYS & _keys(payload)
    properties = payload["tools"][0]["function"]["parameters"]["properties"]
    assert set(properties) == {"temperature", "top_p", "max_tokens"}


def test_anthropic_native_tools_are_unsupported_and_send_nothing() -> None:
    sent: list[httpx.Request] = []
    client = LLMClient(
        _run_model("anthropic"),
        agent_config=AgentConfig(),
        http_transport=httpx.MockTransport(
            lambda request: sent.append(request) or httpx.Response(500)
        ),
    )
    try:
        with pytest.raises(NotImplementedError):
            client.chat_with_tools([{"role": "user", "content": "hello"}], _TOOLS)
    finally:
        client.close()
    assert sent == []

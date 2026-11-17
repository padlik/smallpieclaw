"""Ordinary request knobs stay absent; graph extraction policy stays fixed."""

from __future__ import annotations

import inspect
import json

import httpx
import pytest

from unittest.mock import patch

from config_schema import AgentConfig, ResolvedProviderConnection
from llm_client import LLMClient
from model_registry import ModelMetadata, ModelRecord, RunModel


def _run_model(output_tokens: int | None = 2048) -> RunModel:
    model = ModelRecord(
        "p/model", "p", "model", "Model",
        ModelMetadata(context_tokens=4096, output_tokens=output_tokens),
    )
    connection = ResolvedProviderConnection(
        "p", "openai", "https://api.example.test/v1", 15, "fake-key"
    )
    return RunModel(model, connection, 1, 1)


_TOOLS = [{"type": "function", "function": {"name": "go", "parameters": {"type": "object"}}}]


@pytest.mark.parametrize("mode", ["text", "json", "native"])
def test_ordinary_chat_request_contains_no_generation_tuning_fields(mode: str) -> None:
    requests: list[httpx.Request] = []
    client = LLMClient(
        _run_model(),
        agent_config=AgentConfig(ctx_max_tokens=2048),
        http_transport=httpx.MockTransport(
            lambda request: requests.append(request)
            or httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
        ),
    )
    messages = [{"role": "user", "content": "hello"}]

    try:
        if mode == "native":
            client.chat_with_tools(messages, _TOOLS)
        else:
            client.chat(messages, json_mode=mode == "json")
    finally:
        client.close()

    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    assert not {"max_tokens", "max_completion_tokens", "temperature", "top_p", "options"} & payload.keys()


def test_graph_extraction_policy_is_fixed_without_an_output_limit_gate() -> None:
    requests: list[httpx.Request] = []
    transport = httpx.MockTransport(
        lambda request: requests.append(request)
        or httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})
    )
    client = LLMClient.for_graph_extraction(
        _run_model(), agent_config=AgentConfig(ctx_max_tokens=2048), http_transport=transport
    )

    client.chat([{"role": "user", "content": "extract"}], json_mode=True)

    payload = json.loads(requests[0].content)
    assert payload["max_completion_tokens"] == 1024
    assert payload["temperature"] == 0.1
    client.close()

    # Discovered output metadata is not a gate: a small known limit still builds.
    LLMClient.for_graph_extraction(
        _run_model(output_tokens=512), agent_config=AgentConfig()
    ).close()


def test_public_llm_provider_protocol_signatures_are_unchanged() -> None:
    from interfaces import LLMProvider

    assert tuple(inspect.signature(LLMProvider.chat).parameters) == (
        "self", "messages", "system", "progress_cb", "json_mode"
    )
    assert tuple(inspect.signature(LLMProvider.chat_with_tools).parameters) == (
        "self", "messages", "tools", "system", "progress_cb"
    )


_KNOBS = [("max_tokens", 256), ("temperature", 0.2), ("top_p", 0.9)]


@pytest.mark.parametrize(("knob", "value"), _KNOBS)
def test_client_constructor_rejects_generation_knob(knob: str, value: object) -> None:
    with pytest.raises(TypeError):
        LLMClient(_run_model(), agent_config=AgentConfig(), **{knob: value})


@pytest.mark.parametrize(
    "call", ["chat", "chat_with_fallback", "chat_with_tools", "chat_with_tools_fallback"]
)
@pytest.mark.parametrize(("knob", "value"), _KNOBS)
def test_public_call_rejects_generation_knob_before_send(
    call: str, knob: str, value: object
) -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(500)

    client = LLMClient(
        _run_model(),
        agent_config=AgentConfig(ctx_max_tokens=2048),
        http_transport=httpx.MockTransport(handler),
    )
    messages = [{"role": "user", "content": "hello"}]
    args = (messages, _TOOLS) if "tools" in call else (messages,)
    try:
        with patch.object(
            client,
            "_invoke_provider",
            side_effect=AssertionError("provider invocation was entered"),
        ) as invocation_tripwire:
            with pytest.raises(TypeError):
                getattr(client, call)(*args, **{knob: value})
        invocation_tripwire.assert_not_called()
    finally:
        client.close()

    assert sent == []


def test_anthropic_ordinary_request_carries_only_mandatory_output_cap() -> None:
    requests: list[httpx.Request] = []
    model = ModelRecord(
        "p/model", "p", "model", "Model",
        ModelMetadata(context_tokens=200_000, output_tokens=2048),
    )
    connection = ResolvedProviderConnection(
        "p", "anthropic", "https://api.example.test", 9, "fake-key"
    )
    client = LLMClient(
        RunModel(model, connection, 1, 1),
        agent_config=AgentConfig(),
        http_transport=httpx.MockTransport(
            lambda request: requests.append(request)
            or httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})
        ),
    )

    try:
        assert client.chat([{"role": "user", "content": "hello"}]) == "ok"
    finally:
        client.close()

    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    assert payload["max_tokens"] == 1024  # configured required cap, not the discovered 2048
    assert not {"max_completion_tokens", "temperature", "top_p", "options"} & payload.keys()

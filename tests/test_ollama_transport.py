"""Cloud Ollama wire gating through the installed SDK and MockTransport."""

from __future__ import annotations

import json

import httpx
import pytest

from call_attempt_state import CallAttemptState
from config_schema import AgentConfig, ResolvedProviderConnection
from interfaces import ChatResponse
from llm_client import LLMClient
from model_registry import ModelMetadata, ModelRecord, RunModel
from token_estimator import estimate_request_payload_tokens


def _client(handler) -> LLMClient:
    model = ModelRecord(
        "cloud/model", "cloud", "model", "Cloud model",
        ModelMetadata(context_tokens=8192, output_tokens=2048, tool_calling=True),
    )
    connection = ResolvedProviderConnection(
        "cloud", "ollama", "https://ollama.com", 20, "fake-cloud-key"
    )
    return LLMClient(
        RunModel(model, connection, 1, 1),
        agent_config=AgentConfig(ctx_max_tokens=4096),
        http_transport=httpx.MockTransport(handler),
    )


def _ollama_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "model",
            "created_at": "2026-10-14T00:00:00Z",
            "message": {"role": "assistant", "content": "ok"},
            "done": True,
            "prompt_eval_count": 4,
            "eval_count": 2,
        },
    )


def test_installed_sdk_serializes_exact_pinned_cloud_request_and_counts_once() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _ollama_response()

    client = _client(handler)
    state = CallAttemptState(2, 0)

    with client.bind_attempt(state):
        assert client.chat([{"role": "user", "content": "hello"}]) == "ok"

    assert len(captured) == 1
    request = captured[0]
    assert request.method == "POST"
    assert str(request.url) == "https://ollama.com/api/chat"
    assert request.headers["authorization"] == "Bearer fake-cloud-key"
    payload = json.loads(request.content)
    assert payload["model"] == "model"
    assert payload["stream"] is False
    assert payload["messages"] == [{"role": "user", "content": "hello"}]
    assert "options" not in payload
    assert state.outbound_attempts == 1
    client.close()


def test_ollama_preview_is_captured_at_sdk_hook_without_transport_or_count() -> None:
    captured: list[httpx.Request] = []
    client = _client(lambda request: captured.append(request) or _ollama_response())

    preview = client.preview_request([{"role": "user", "content": "hello"}])

    assert preview.canonical_model_id == "cloud/model"
    assert preview.input_tokens > 0
    assert captured == []
    client.close()


def test_native_tool_wire_preserves_full_schema_and_snapshots_caller_tools() -> None:
    captured: list[httpx.Request] = []
    tools = [{
        "type": "function",
        "function": {
            "name": "lookup",
            "description": "lookup café",
            "x-function-extension": {"retained": True},
            "parameters": {
                "type": "object",
                "properties": {
                    "value": {
                        "type": "string",
                        "pattern": "^[a-z]+$",
                        "minimum": 2,
                        "nullable": True,
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                        "$ref": "#/$defs/Value",
                        "x-property-extension": {"retained": True},
                    }
                },
                "required": ["value"],
                "additionalProperties": False,
                "$defs": {"Value": {"type": "string", "enum": ["café", "tea"]}},
            },
        },
        "x-tool-extension": {"retained": True},
    }]
    expected_tools = json.loads(json.dumps(tools, ensure_ascii=False, allow_nan=False))
    messages = [{"role": "user", "content": "find"}]

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        # Mutating the caller's object after finalization must not mutate the body.
        tools[0]["function"]["parameters"]["properties"]["value"]["pattern"] = "changed"
        return _ollama_response()

    client = _client(handler)
    preview = client.preview_request(messages, tools=tools, definition_revision=17)
    assert preview.mode == "native"
    assert captured == []
    state = CallAttemptState(2, 0)
    with client.bind_attempt(state, definition_revision=17):
        response = client.chat_with_tools(messages, tools)

    assert isinstance(response, ChatResponse)
    assert json.loads(captured[0].content)["tools"] == expected_tools
    assert preview.input_tokens == estimate_request_payload_tokens(
        captured[0].content.decode("utf-8"), model="model", definition_revision=17
    )
    assert state.mode == "native"
    assert state.outbound_attempts == 1
    client.close()


def test_large_native_schema_is_sent_unchanged_and_counted_once() -> None:
    captured: list[httpx.Request] = []
    client = _client(lambda request: captured.append(request) or _ollama_response())
    state = CallAttemptState(1, 0)
    tools = [{
        "type": "function",
        "function": {
            "name": "large",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "string", "description": "x" * 100_000}},
            },
        },
    }]
    with client.bind_attempt(state):
        client.chat_with_tools([{"role": "user", "content": "call"}], tools)

    assert len(captured) == 1
    assert json.loads(captured[0].content)["tools"] == tools
    assert state.outbound_attempts == 1
    client.close()


def test_empty_native_tools_remain_native_for_preview_and_send() -> None:
    captured: list[httpx.Request] = []
    client = _client(lambda request: captured.append(request) or _ollama_response())

    preview = client.preview_request([{"role": "user", "content": "hello"}], tools=[])
    assert preview.mode == "native"
    assert captured == []

    state = CallAttemptState(1, 0)
    with client.bind_attempt(state):
        result = client.chat_with_tools([{"role": "user", "content": "hello"}], [])

    assert isinstance(result, ChatResponse)
    assert json.loads(captured[0].content)["tools"] == []
    assert state.mode == "native"
    assert state.outbound_attempts == 1
    client.close()


def test_json_mode_and_empty_native_mode_are_not_inferred_from_tools_truthiness() -> None:
    captured: list[httpx.Request] = []
    client = _client(lambda request: captured.append(request) or _ollama_response())

    json_state = CallAttemptState(1, 0)
    with client.bind_attempt(json_state):
        assert client.chat([{"role": "user", "content": "json"}], json_mode=True) == "ok"
    native_state = CallAttemptState(1, 0)
    with client.bind_attempt(native_state):
        client.chat_with_tools([{"role": "user", "content": "native"}], [])

    assert json_state.mode == "json"
    assert native_state.mode == "native"
    assert json.loads(captured[0].content)["tools"] == []
    assert json.loads(captured[1].content)["tools"] == []
    client.close()


def test_ollama_redirect_is_not_followed() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(307, headers={"Location": "https://attacker.invalid/api/chat"})

    client = _client(handler)
    state = CallAttemptState(1, 0)

    with client.bind_attempt(state), pytest.raises(Exception):
        client.chat([{"role": "user", "content": "hello"}])

    assert len(captured) == 1
    assert str(captured[0].url) == "https://ollama.com/api/chat"
    assert state.outbound_attempts == 1
    client.close()


def test_ollama_custom_or_invalid_origin_is_rejected_before_client_construction() -> None:
    model = ModelRecord("cloud/model", "cloud", "model", "Cloud model")
    connection = ResolvedProviderConnection(
        "cloud", "ollama", "https://attacker.invalid", 20, "fake-cloud-key"
    )

    with pytest.raises(Exception, match="official https://ollama.com"):
        LLMClient(
            RunModel(model, connection, 1, 1),
            agent_config=AgentConfig(),
            http_transport=httpx.MockTransport(lambda request: _ollama_response()),
        )

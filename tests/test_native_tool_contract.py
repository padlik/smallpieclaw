"""Native tool payloads retain the complete caller-supplied definition graph."""

from __future__ import annotations

import json

import httpx

from call_attempt_state import CallAttemptState
from config_schema import AgentConfig, ResolvedProviderConnection
from interfaces import ChatResponse
from llm_client import LLMClient
from model_registry import ModelMetadata, ModelRecord, RunModel


def test_native_tool_definition_payload_is_not_projected_or_duplicated() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"tool_calls": [
                {"id": "call-1", "function": {"name": "plan_step", "arguments": "{\"step\":1}"}}
            ]}}]},
        )

    model = ModelRecord(
        "p/vendor/model", "p", "vendor/model", "Display",
        ModelMetadata(context_tokens=8192, output_tokens=2048, tool_calling=True),
    )
    connection = ResolvedProviderConnection(
        "p", "openrouter", "https://router.example.test/prefix/api/v1", 22, "fake-key"
    )
    client = LLMClient(
        RunModel(model, connection, 4, 2),
        agent_config=AgentConfig(ctx_max_tokens=4096),
        http_transport=httpx.MockTransport(handler),
    )
    definitions = [
        {"type": "function", "function": {
            "name": "builtin", "description": "built in", "parameters": {
                "type": "object", "properties": {"arg": {"type": "string", "x-constraint": {"a": [1, 2]}}}
            }
        }},
        {"type": "function", "function": {
            "name": "mcp__service__tool", "description": "MCP", "parameters": {
                "type": "object", "required": ["payload"], "properties": {
                    "payload": {"type": "array", "items": {"type": "object", "additionalProperties": True}}
                }
            }
        }},
    ]
    state = CallAttemptState(2, 0)

    with client.bind_attempt(state, definition_revision="defs-10"):
        response = client.chat_with_tools([{"role": "user", "content": "use tool"}], definitions)

    assert isinstance(response, ChatResponse)
    assert response.tool_calls[0].arguments == {"step": 1}
    assert len(captured) == 1
    wire = json.loads(captured[0].content)
    assert wire["model"] == "vendor/model"
    assert wire["tools"] == definitions
    assert wire["tool_choice"] == "auto"
    assert state.outbound_attempts == 1
    assert captured[0].url.path == "/prefix/api/v1/chat/completions"
    client.close()

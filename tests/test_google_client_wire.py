"""Wire-level tests for the pinned Google provider adapter."""

from __future__ import annotations

import base64
import json

import httpx

from config_schema import AgentConfig, ResolvedProviderConnection
from interfaces import ChatResponse
from llm_client import LLMClient
from model_registry import ModelMetadata, ModelRecord, RunModel
from token_usage import TokenUsageRegistry


def _client(
    transport: httpx.BaseTransport,
    *,
    usage_registry: TokenUsageRegistry | None = None,
) -> LLMClient:
    """Create a Google client with an in-process HTTP transport."""
    model = ModelRecord(
        "google/test-model",
        "google",
        "test-model",
        "Test model",
        ModelMetadata(vision=True, tool_calling=True),
    )
    connection = ResolvedProviderConnection(
        provider_id="google",
        adapter="google",
        base_url="https://google.example.test/v1beta",
        request_timeout=5,
        api_key="fake-google-key",
    )
    return LLMClient(
        RunModel(model, connection, catalog_revision=1, selection_revision=1),
        agent_config=AgentConfig(ctx_max_tokens=4096),
        http_transport=transport,
        usage_registry=usage_registry,
    )


def test_text_request_has_supported_google_parts_system_instruction_and_json_mode() -> None:
    captured: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "{}"}]}}]},
        )

    client = _client(httpx.MockTransport(respond))
    try:
        assert client.chat(
            [
                {"role": "user", "content": "hello", "metadata": {"role": "user"}},
                {"role": "assistant", "content": "hi"},
            ],
            system="Follow rules",
            json_mode=True,
        ) == "{}"

        assert len(captured) == 1
        assert json.loads(captured[0].content) == {
            "contents": [
                {"role": "user", "parts": [{"text": "hello"}]},
                {"role": "model", "parts": [{"text": "hi"}]},
            ],
            "systemInstruction": {"parts": [{"text": "Follow rules"}]},
            "generationConfig": {"responseMimeType": "application/json"},
        }
    finally:
        client.close()


def test_image_part_uses_google_inline_data_wire_shape(tmp_path) -> None:
    image = tmp_path / "sample.png"
    image.write_bytes(b"fake-image-bytes")
    captured: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "seen"}]}}]},
        )

    client = _client(httpx.MockTransport(respond))
    try:
        assert client.chat([{"role": "user", "content": "What is this?", "images": [str(image)]}]) == "seen"
        body = json.loads(captured[0].content)
        assert body == {
            "contents": [{
                "role": "user",
                "parts": [
                    {"text": "What is this?"},
                    {
                        "inlineData": {
                            "mimeType": "image/png",
                            "data": base64.b64encode(b"fake-image-bytes").decode("ascii"),
                        }
                    },
                ],
            }]
        }
    finally:
        client.close()


def test_native_response_usage_updates_client_and_registry_from_native_wire() -> None:
    captured: list[httpx.Request] = []
    registry = TokenUsageRegistry()
    tools = [{
        "type": "function",
        "function": {"name": "lookup", "parameters": {"type": "object"}},
    }]

    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"tool_calls": [{
                    "id": "call-1",
                    "function": {"name": "lookup", "arguments": "{}"},
                }]}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            },
        )

    client = _client(httpx.MockTransport(respond), usage_registry=registry)
    try:
        response = client.chat_with_tools([{"role": "user", "content": "look it up"}], tools)

        assert isinstance(response, ChatResponse)
        assert response.is_tool_call
        assert len(captured) == 1
        body = json.loads(captured[0].content)
        assert body["messages"] == [{"role": "user", "content": "look it up"}]
        assert body["tools"] == tools
        assert client.get_today_usage()["prompt_tokens"] == 7
        assert client.get_today_usage()["completion_tokens"] == 3
        assert registry.get_today()["google/test-model"] == {
            "prompt": 7,
            "completion": 3,
            "total": 10,
        }
    finally:
        client.close()

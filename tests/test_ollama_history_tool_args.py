"""Ollama tool history is validated against the SDK's actual HTTP wire body."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from model_registry import ModelMetadata
from tests.run_model_fixtures import make_run_model_client


def _history(arguments_wire_value: Any) -> list[dict]:
    """Build a history containing a native tool call and its result."""
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "shell", "arguments": arguments_wire_value},
            }],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "exit 0"},
    ]


def _response_body(tool_calls: list[dict] | None = None) -> dict:
    """Create an Ollama API response body accepted by the installed SDK."""
    return {
        "model": "llama3",
        "created_at": "2026-10-14T00:00:00Z",
        "message": {
            "role": "assistant",
            "content": "done",
            "tool_calls": tool_calls or [],
        },
        "done": True,
        "prompt_eval_count": 0,
        "eval_count": 0,
    }


@pytest.fixture
def client_factory() -> Callable[..., tuple[Any, list[httpx.Request]]]:
    """Create pinned Ollama clients and close each SDK-owned transport after test."""
    clients = []

    def create(response_body: dict | None = None) -> tuple[Any, list[httpx.Request]]:
        captured: list[httpx.Request] = []
        body = response_body or _response_body()

        def respond(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json=body)

        client = make_run_model_client(
            "ollama",
            httpx.MockTransport(respond),
            metadata=ModelMetadata(
                context_tokens=8192,
                output_tokens=1024,
                vision=False,
                tool_calling=True,
            ),
            upstream_id="llama3",
        )
        clients.append(client)
        return client, captured

    yield create
    for client in clients:
        client.close()


def _sent_messages(captured: list[httpx.Request]) -> list[dict]:
    """Read the messages from the actual captured SDK request body."""
    assert len(captured) == 1
    return json.loads(captured[0].content)["messages"]


def _sent_args(captured: list[httpx.Request]) -> object:
    """Extract one tool-call argument value from the actual HTTP request."""
    messages = _sent_messages(captured)
    assistant = next(message for message in messages if message.get("tool_calls"))
    return assistant["tool_calls"][0]["function"]["arguments"]


class TestOllamaHistoryArgumentsNormalization:
    def test_json_string_normalized_to_dict(self, client_factory):
        client, captured = client_factory()
        client.chat_with_tools(
            _history(json.dumps({"query": "PTO approval pending SharePoint list"})),
            tools=[],
            system=None,
        )
        args = _sent_args(captured)
        assert isinstance(args, dict), f"expected dict, got {type(args).__name__}: {args!r}"
        assert args == {"query": "PTO approval pending SharePoint list"}

    @pytest.mark.parametrize(
        "wire",
        ["null", "[]", "42", "{", ""],
        ids=["json-null", "json-array", "json-int", "malformed", "empty"],
    )
    def test_non_dict_wire_values_coerced_to_empty_dict(self, client_factory, wire: str):
        client, captured = client_factory()
        client.chat_with_tools(_history(wire), tools=[], system=None)
        assert _sent_args(captured) == {}

    @pytest.mark.parametrize("wire", [None, 123, []], ids=["none", "int", "list"])
    def test_non_string_python_values_coerced_to_empty_dict(self, client_factory, wire):
        client, captured = client_factory()
        client.chat_with_tools(_history(wire), tools=[], system=None)
        assert _sent_args(captured) == {}

    def test_already_parsed_dict_passes_through(self, client_factory):
        client, captured = client_factory()
        client.chat_with_tools(_history({"command": "df -h"}), tools=[], system=None)
        assert _sent_args(captured) == {"command": "df -h"}

    def test_shared_messages_not_mutated(self, client_factory):
        original = json.dumps({"query": "x"})
        messages = _history(original)
        client, captured = client_factory()
        client.chat_with_tools(messages, tools=[], system=None)
        assert _sent_args(captured) == {"query": "x"}
        assert messages[0]["tool_calls"][0]["function"]["arguments"] == original

    def test_normalized_payload_passes_real_ollama_message_validation(self, client_factory):
        import ollama._types as ollama_types

        client, captured = client_factory()
        client.chat_with_tools(
            _history(json.dumps({"query": "validate me"})),
            tools=[],
            system=None,
        )
        for message in _sent_messages(captured):
            ollama_types.Message.model_validate(message)

    def test_turn1_tool_call_response_is_parsed(self, client_factory):
        client, _captured = client_factory(_response_body([{
            "function": {"name": "shell", "arguments": {"cmd": "ls"}},
        }]))
        result = client.chat_with_tools([], tools=[], system=None)
        assert result.tool_calls is not None
        assert result.tool_calls[0].name == "shell"
        assert result.tool_calls[0].arguments == {"cmd": "ls"}

    def test_unmatched_tool_call_id_still_valid_ollama_message(self, client_factory):
        import ollama._types as ollama_types

        client, captured = client_factory()
        client.chat_with_tools(
            [{"role": "tool", "tool_call_id": "call_ghost", "content": "ok"}],
            tools=[],
            system=None,
        )
        tool_message = next(
            message for message in _sent_messages(captured) if message["role"] == "tool"
        )
        assert "tool_name" not in tool_message
        ollama_types.Message.model_validate(tool_message)

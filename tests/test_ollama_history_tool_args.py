"""Regression tests for _ollama_chat_with_tools() message history normalization.

The native tool-call feedback stores arguments as json.dumps(dict) for the OpenAI
HTTP wire format. The Ollama SDK Pydantic Message model requires arguments to be a
dict. These tests verify that _ollama_chat_with_tools() normalizes the history
before handing it to client.chat().
"""
import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from llm_client import LLMClient


def _make_client() -> LLMClient:
    """Build an LLMClient configured for Ollama and inject a mock SDK client."""
    client = LLMClient(
        {
            "models": [
                {
                    "name": "test-ollama",
                    "provider": "ollama",
                    "model": "llama3",
                    "api_key": "ignored",
                    "base_url": "http://localhost:11434",
                }
            ],
            "agent": {"default_model": "llama3", "fallback_models": []},
        }
    )
    mock_sdk = MagicMock()
    resp = MagicMock()
    # tool_calls must be a falsy empty list so the code falls through to text path.
    resp.message.tool_calls = []
    # content must be a real str -- .strip() is called on it.
    resp.message.content = "done"
    resp.message.thinking = ""
    # Usage counters must be real ints -- arithmetic is performed on them.
    resp.eval_count = 0
    resp.prompt_eval_count = 0
    mock_sdk.chat.return_value = resp
    client._ollama_clients = [mock_sdk]
    client._active_idx = 0
    return client


def _history(arguments_wire_value) -> list[dict]:
    """Build a 2-turn history: native tool-call + tool result."""
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "shell", "arguments": arguments_wire_value},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "exit 0"},
    ]


def _sent_args(client: LLMClient) -> object:
    """Extract the arguments value that was sent to the Ollama SDK."""
    mock_sdk: Any = client._ollama_clients[0]
    sent: list[dict] = mock_sdk.chat.call_args.kwargs["messages"]
    asst = next(m for m in sent if m.get("tool_calls"))
    return asst["tool_calls"][0]["function"]["arguments"]


class TestOllamaHistoryArgumentsNormalization:
    def test_json_string_normalized_to_dict(self):
        """Core regression: JSON string arguments must become a dict."""
        client = _make_client()
        client._ollama_chat_with_tools(
            _history(json.dumps({"query": "PTO approval pending SharePoint list"})),
            tools=[],
            system=None,
        )
        args = _sent_args(client)
        assert isinstance(args, dict), f"expected dict, got {type(args).__name__}: {args!r}"
        assert args == {"query": "PTO approval pending SharePoint list"}

    @pytest.mark.parametrize(
        "wire",
        [
            "null",       # json null -> None -> {}
            "[]",         # json array -> list -> {}
            "42",         # json int -> int -> {}
            "{",          # malformed JSON -> JSONDecodeError -> {}
            "",           # empty string -> JSONDecodeError -> {}
        ],
    )
    def test_non_dict_wire_values_coerced_to_empty_dict(self, wire: str):
        client = _make_client()
        client._ollama_chat_with_tools(_history(wire), tools=[], system=None)
        assert _sent_args(client) == {}

    @pytest.mark.parametrize(
        "wire",
        [
            None,   # Python None (not a string) -> {}
            123,    # Python int -> {}
            [],     # Python list -> {}
        ],
    )
    def test_non_string_python_values_coerced_to_empty_dict(self, wire):
        client = _make_client()
        client._ollama_chat_with_tools(_history(wire), tools=[], system=None)
        assert _sent_args(client) == {}

    def test_already_parsed_dict_passes_through(self):
        """Arguments already a dict must not be double-processed."""
        client = _make_client()
        client._ollama_chat_with_tools(
            _history({"command": "df -h"}), tools=[], system=None
        )
        assert _sent_args(client) == {"command": "df -h"}

    def test_shared_messages_not_mutated(self):
        """The fix must not mutate the caller's messages list."""
        original = json.dumps({"query": "x"})
        messages = _history(original)
        client = _make_client()
        client._ollama_chat_with_tools(messages, tools=[], system=None)
        assert messages[0]["tool_calls"][0]["function"]["arguments"] == original

    def test_normalized_payload_passes_real_ollama_validation(self) -> None:
        """Verify the normalized payload satisfies the Ollama SDK Pydantic model.

        The mock-based tests verify we send a dict; this test exercises the actual
        SDK validation that raised in production.
        """
        import ollama._types as _ollama_types

        client = _make_client()
        client._ollama_chat_with_tools(
            _history(json.dumps({"query": "validate me"})),
            tools=[],
            system=None,
        )
        mock_sdk: Any = client._ollama_clients[0]
        sent: list[dict] = mock_sdk.chat.call_args.kwargs["messages"]
        for m in sent:
            _ollama_types.Message.model_validate(m)

"""H2: native tool-call ``arguments`` parsing must never yield a non-dict.

Adversarial models (or buggy providers) can return an ``arguments`` field that is
not a JSON object: the JSON literals ``null`` / ``[]`` / ``42`` / ``"str"``,
malformed JSON, or a non-string value entirely. Left unguarded, ``json.loads``
returns ``None`` / list / int / str (or raises ``TypeError``), and the resulting
non-dict flows into ``ToolCall.arguments`` where the ReAct loop calls ``.get()``
on it and aborts the run with an ``AttributeError``.

Both the OpenAI and Google native parsers must coerce any non-dict to ``{}``
(matching the existing Ollama guard). These tests drive the real parser methods
with the HTTP layer mocked at ``client._http`` — no network.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from llm_client import LLMClient


def _make_client(provider: str) -> LLMClient:
    """Build a real LLMClient for *provider* with fake credentials (no network)."""
    model = {
        "name": "m", "provider": provider, "model": "m",
        "api_key": "k", "base_url": "https://example.invalid/v1",
    }
    return LLMClient({
        "models": [model],
        "agent": {"default_model": "m", "fallback_models": []},
    })


def _tool_call_body(arguments) -> dict:
    """An OpenAI-format response body carrying one tool call whose function
    ``arguments`` field is *arguments* exactly as it would arrive on the wire."""
    return {
        "choices": [{
            "message": {
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "shell", "arguments": arguments},
                }],
            },
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


def _invoke(provider: str, body: dict):
    """Call the provider's native tool parser with *body* mocked at the HTTP seam."""
    client = _make_client(provider)
    resp_obj = MagicMock()
    resp_obj.json.return_value = body
    resp_obj.raise_for_status.return_value = None
    client._http = MagicMock()
    client._http.post.return_value = resp_obj
    messages = [{"role": "user", "content": "hi"}]
    if provider == "openai":
        return client._openai_chat_with_tools(messages, tools=[], system=None)
    return client._google_chat_with_tools(messages, tools=[], system=None)


# (label, wire value for the arguments field)
_NON_DICT_JSON = [
    ("json_null", "null"),
    ("json_array", "[]"),
    ("json_int", "42"),
    ("json_string", '"hello"'),
    ("json_bool", "true"),
    ("malformed_json", "{"),
    ("empty_string", ""),
]

_NON_STRING = [
    ("python_none", None),
    ("python_int", 123),
    ("python_list", ["a"]),
    ("already_parsed_dict", {"command": "ls"}),
]


@pytest.mark.parametrize("provider", ["openai", "google"])
@pytest.mark.parametrize("label,arguments", _NON_DICT_JSON, ids=[c[0] for c in _NON_DICT_JSON])
def test_non_dict_json_arguments_coerced_to_empty_dict(provider, label, arguments):
    resp = _invoke(provider, _tool_call_body(arguments))
    assert resp.is_tool_call
    assert resp.tool_calls  # native parser must populate tool_calls (narrows Optional)
    tc = resp.tool_calls[0]
    assert tc.name == "shell"
    assert tc.arguments == {}, f"{provider}/{label}: expected {{}} got {tc.arguments!r}"


@pytest.mark.parametrize("provider", ["openai", "google"])
@pytest.mark.parametrize("label,arguments", _NON_STRING, ids=[c[0] for c in _NON_STRING])
def test_non_string_arguments_coerced_to_empty_dict(provider, label, arguments):
    # A non-string arguments value makes json.loads raise TypeError; the guard
    # must catch it and fall back to {} rather than letting it abort the run.
    resp = _invoke(provider, _tool_call_body(arguments))
    assert resp.is_tool_call
    assert resp.tool_calls  # native parser must populate tool_calls (narrows Optional)
    assert resp.tool_calls[0].arguments == {}, f"{provider}/{label}"


@pytest.mark.parametrize("provider", ["openai", "google"])
def test_valid_object_arguments_still_parse(provider):
    # Regression guard: the non-dict coercion must not break the happy path.
    resp = _invoke(provider, _tool_call_body('{"command": "df -h"}'))
    assert resp.is_tool_call
    assert resp.tool_calls  # native parser must populate tool_calls (narrows Optional)
    assert resp.tool_calls[0].arguments == {"command": "df -h"}

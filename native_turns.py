"""OpenAI native tool-call wire-shape helpers.

Extracted from ``react_loop.py``: appending native tool-result messages
and linearizing interleaved native tool-call turns into a flat
assistant/tool message list. ``react_loop`` re-exports both names for
backward compatibility.
"""

import json
import secrets

from interfaces import ToolCall
from tool_formatting import _compact_args_repr


def _append_native_tool_result(messages: list[dict], tc: ToolCall, content: str) -> None:
    """Append the assistant tool-call turn and its matching tool-result message.

    Native multi-turn dispatch requires the OpenAI wire shape: an assistant
    message carrying the ``tool_calls`` entry, immediately followed by a ``tool``
    message keyed by the same ``tool_call_id``. Centralising this keeps every
    intercept site (standard tool, plan, vision_query) identical.

    Guards two provider-rejection cases: an empty ``tc.id`` (some models omit it)
    is replaced with a generated ``call_<hex>`` id so the assistant and tool
    turns stay linked, and a non-string ``content`` is coerced to ``""`` because
    OpenAI 400s on ``content: null`` in a ``role:"tool"`` message.
    """
    call_id = tc.id or f"call_{secrets.token_hex(4)}"
    if not isinstance(content, str):
        content = ""
    messages.append({"role": "assistant", "content": None, "tool_calls": [{
        "id": call_id, "type": "function",
        "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
    }]})
    messages.append({"role": "tool", "tool_call_id": call_id, "content": content})


def _linearize_native_turns(messages: list[dict]) -> list[dict]:
    """Flatten native tool-calling turns into plain text for the json_mode path.

    Native multi-turn dispatch writes OpenAI wire-format messages into the shared
    ``messages`` list (see ``_append_native_tool_result``): an assistant message
    carrying ``tool_calls`` with ``content: None``, immediately followed by a
    ``tool`` message keyed by ``tool_call_id``. The provider ``chat`` backends
    used by the json_mode fallback only preserve ``role`` and ``content``,
    dropping ``tool_calls`` and
    ``tool_call_id``. Sending those stripped messages produces malformed payloads
    (an assistant with ``content: null`` and no ``tool_calls``, an orphan
    ``role: "tool"`` with no ``tool_call_id``) that providers reject with a 400,
    aborting the run.

    This returns a *new* list in which native-format turns are converted to plain
    text the json_mode builders can serialize safely:

    - Assistant messages with ``tool_calls`` become
      ``{"role": "assistant", "content": "Called tool: <name>(<args_summary>)"}``.
    - ``tool`` messages become ``{"role": "user", "content": <tool_result>}``.

    All other messages pass through unchanged. Conversion is 1:1, so the message
    count is preserved and any goal-index anchor into the list stays valid. It is
    also idempotent: already-linearized (plain) messages have no native fields and
    pass through untouched.
    """
    linearized: list[dict] = []
    for m in messages:
        role = m.get("role")
        tool_calls = m.get("tool_calls")
        if role == "assistant" and tool_calls:
            parts = []
            for tc in tool_calls:
                func = tc.get("function") or {}
                name = func.get("name", "tool")
                raw_args = func.get("arguments", "")
                try:
                    parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except (json.JSONDecodeError, TypeError):
                    parsed_args = {}
                if not isinstance(parsed_args, dict):
                    parsed_args = {}
                parts.append(f"{name}({_compact_args_repr(name, parsed_args)})")
            linearized.append({
                "role": "assistant",
                "content": "Called tool: " + ", ".join(parts),
            })
        elif role == "tool":
            linearized.append({
                "role": "user",
                "content": m.get("content") or "",
            })
        else:
            linearized.append(m)
    return linearized

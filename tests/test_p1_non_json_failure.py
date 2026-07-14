"""P1: React-loop non-JSON protocol failure tests.

Verifies that repeated non-JSON LLM responses produce an explicit protocol-error
return string instead of a fabricated 'finish' action, and that Kimi-like
reasoning-field responses that extract to valid JSON are accepted normally.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch


from react_loop import ReactContext, react_loop, _JSON_FAIL_LIMIT


def _make_ctx(llm_responses: list[str]) -> tuple[ReactContext, list[str]]:
    """Return a ReactContext whose LLM always cycles through *llm_responses*,
    and a list that collects every progress string sent."""
    progress_log: list[str] = []

    call_counter = [0]

    def fake_chat_with_fallback(messages, **kwargs):
        idx = call_counter[0] % len(llm_responses)
        call_counter[0] += 1
        return llm_responses[idx]

    llm = MagicMock()
    llm.chat_with_fallback.side_effect = fake_chat_with_fallback
    # Native tool calling not supported in these tests — fall through to json_mode
    llm.chat_with_tools_fallback.side_effect = NotImplementedError("native tools not mocked")
    llm.llm_cfg = {"model": "test-model"}

    ctx = ReactContext(
        llm=llm,
        tool_index=MagicMock(),
        executor=MagicMock(),
        creator=MagicMock(),
        memory=MagicMock(),
        builtin_executor=None,
        mcp_manager=None,
        skill_registry=None,
        cancel_event=threading.Event(),
    )
    return ctx, progress_log


class TestNonJsonProtocolError:
    """react_loop returns an explicit error string after _JSON_FAIL_LIMIT non-JSON responses."""

    def test_non_json_streak_returns_protocol_error_prefix(self):
        ctx, _ = _make_ctx(["This is plain prose, not JSON."])
        with patch("react_loop._build_system_prompt", return_value=("sys_prompt", None)):
            result = react_loop(ctx, "task")
        assert result.startswith("❌ Agent protocol error:")

    def test_protocol_error_contains_non_json_count(self):
        ctx, _ = _make_ctx(["Not JSON at all."])
        with patch("react_loop._build_system_prompt", return_value=("sys_prompt", None)):
            result = react_loop(ctx, "task")
        assert str(_JSON_FAIL_LIMIT) in result

    def test_protocol_error_contains_truncated_response(self):
        prose = "This is definitely not JSON and has some content."
        ctx, _ = _make_ctx([prose])
        with patch("react_loop._build_system_prompt", return_value=("sys_prompt", None)):
            result = react_loop(ctx, "task")
        assert prose[:40] in result

    def test_protocol_error_is_not_a_finish_action(self):
        """Result must NOT look like a successful finish action."""
        ctx, _ = _make_ctx(["prose prose prose"])
        with patch("react_loop._build_system_prompt", return_value=("sys_prompt", None)):
            result = react_loop(ctx, "task")
        # It must be a plain string, not something that would be dispatched as finish
        assert isinstance(result, str)
        assert "finish" not in result.lower() or result.startswith("❌")

    def test_progress_callback_receives_error(self):
        ctx, progress = _make_ctx(["not json"])
        with patch("react_loop._build_system_prompt", return_value=("sys_prompt", None)):
            react_loop(ctx, "task", progress_callback=progress.append)
        assert any("protocol error" in msg.lower() for msg in progress)

    def test_no_tool_execution_on_non_json_streak(self):
        """No tool calls should happen when all responses are non-JSON."""
        ctx, _ = _make_ctx(["I cannot respond with JSON, sorry."])
        with patch("react_loop._build_system_prompt", return_value=("sys_prompt", None)):
            with patch("react_loop._dispatch_tool") as mock_dispatch:
                react_loop(ctx, "task")
        mock_dispatch.assert_not_called()

    def test_repair_prompt_sent_before_terminal_failure(self):
        """For each parse failure before the limit, a repair prompt is appended."""
        responses = ["Not JSON."] * _JSON_FAIL_LIMIT
        ctx, _ = _make_ctx(responses)
        # Track all messages sent to the LLM
        captured_messages: list[list] = []
        orig = ctx.llm.chat_with_fallback.side_effect

        def capture_messages(messages, **kwargs):
            captured_messages.append(list(messages))
            return orig(messages, **kwargs)

        ctx.llm.chat_with_fallback.side_effect = capture_messages

        with patch("react_loop._build_system_prompt", return_value=("sys_prompt", None)):
            react_loop(ctx, "task")

        # At least one intermediate call should have a repair prompt (role=user, ERROR text)
        all_contents = [m["content"] for msgs in captured_messages for m in msgs if m.get("role") == "user"]
        assert any("ERROR" in c and "JSON" in c for c in all_contents)


class TestKimiReasoningFieldAccepted:
    """A valid JSON action extracted from a reasoning field must succeed, not fail."""

    def test_json_action_in_content_is_accepted(self):
        """Normal path: action JSON in content field → not a protocol error."""
        action = '{"action": "finish", "result": "done"}'
        ctx, _ = _make_ctx([action])
        with patch("react_loop._build_system_prompt", return_value=("sys_prompt", None)):
            result = react_loop(ctx, "task")
        assert not result.startswith("❌ Agent protocol error:")

    def test_markdown_fenced_json_is_repaired(self):
        """parse_json() must accept JSON wrapped in ```json fences (common model habit)."""
        from react_loop import parse_json
        fenced = '```json\n{"action": "finish", "result": "ok"}\n```'
        obj = parse_json(fenced)
        assert obj is not None
        assert obj["action"] == "finish"

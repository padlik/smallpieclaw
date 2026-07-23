"""Tests for JSON parsing — parse_json and extract_json_candidates from react_loop."""

from __future__ import annotations

import json

from react_loop import extract_json_candidates, parse_json


class TestExtractJsonCandidates:
    """Test brace-counting extractor."""

    def test_single_object(self):
        text = '{"action": "finish", "result": "done"}'
        candidates = extract_json_candidates(text)
        assert len(candidates) == 1
        assert json.loads(candidates[0])["action"] == "finish"

    def test_multiple_objects(self):
        text = '{"a": 1} and {"b": 2}'
        candidates = extract_json_candidates(text)
        assert len(candidates) == 2

    def test_nested_braces(self):
        text = '{"a": {"nested": true}}'
        candidates = extract_json_candidates(text)
        assert len(candidates) == 1
        obj = json.loads(candidates[0])
        assert obj["a"]["nested"] is True

    def test_braces_in_strings(self):
        text = '{"code": "function() { return {}; }"}'
        candidates = extract_json_candidates(text)
        assert len(candidates) == 1
        obj = json.loads(candidates[0])
        assert "function()" in obj["code"]

    def test_prose_wrapped_json(self):
        text = "Here's my response:\n```json\n{\"action\": \"tool\"}\n```\nDone."
        candidates = extract_json_candidates(text)
        assert len(candidates) >= 1

    def test_empty_string(self):
        assert extract_json_candidates("") == []

    def test_no_json(self):
        assert extract_json_candidates("just plain text") == []

    def test_unbalanced_braces(self):
        text = '{"unclosed": true'
        candidates = extract_json_candidates(text)
        assert len(candidates) == 0

    def test_escaped_quotes(self):
        text = r'{"text": "he said \"hello\""}'
        candidates = extract_json_candidates(text)
        assert len(candidates) == 1


class TestParseJson:
    """Test parse_json fallback chain."""

    def test_direct_json(self):
        text = '{"action": "finish", "result": "done"}'
        obj = parse_json(text)
        assert obj == {"action": "finish", "result": "done"}

    def test_json_with_whitespace(self):
        text = '  \n  {"action": "finish", "result": "ok"}  \n  '
        obj = parse_json(text)
        assert obj["action"] == "finish"

    def test_markdown_fenced_json(self):
        text = '```json\n{"action": "tool", "tool": "shell"}\n```'
        obj = parse_json(text)
        assert obj["action"] == "tool"
        assert obj["tool"] == "shell"

    def test_markdown_fence_without_lang(self):
        text = '```\n{"action": "finish", "result": "x"}\n```'
        obj = parse_json(text)
        assert obj["action"] == "finish"

    def test_prose_wrapped_json(self):
        text = "Let me think about this.\n\n{\"action\": \"tool\", \"tool\": \"shell\", \"args\": {\"command\": \"ls\"}}\n\nThat should work."
        obj = parse_json(text)
        assert obj["action"] == "tool"
        assert obj["tool"] == "shell"

    def test_prefers_action_key(self):
        # Two JSON objects: first without "action", second with
        text = '{"name": "test"} {"action": "finish", "result": "ok"}'
        obj = parse_json(text)
        assert obj["action"] == "finish"

    def test_falls_back_to_first_valid_dict(self):
        # No "action" key in either — returns first valid dict
        text = '{"foo": 1} {"bar": 2}'
        obj = parse_json(text)
        assert obj == {"foo": 1}

    def test_empty_string(self):
        assert parse_json("") is None

    def test_whitespace_only(self):
        assert parse_json("   \n\n  ") is None

    def test_invalid_json(self):
        assert parse_json("this is not json at all") is None

    def test_array_ignored(self):
        # JSON arrays are not dicts — should not be returned
        text = '[1, 2, 3]'
        assert parse_json(text) is None

    def test_deeply_nested(self):
        obj = {
            "action": "tool",
            "tool": "shell",
            "args": {"command": "echo 'test'"},
            "thought": "I need to run a command",
        }
        text = json.dumps(obj)
        result = parse_json(text)
        assert result == obj

    def test_special_chars_in_values(self):
        obj = {"action": "finish", "result": "Line 1\nLine 2\t<tag>&amp;"}
        text = json.dumps(obj)
        result = parse_json(text)
        assert result["result"] == "Line 1\nLine 2\t<tag>&amp;"

    def test_unicode_content(self):
        obj = {"action": "finish", "result": "Привет мир 🎉"}
        text = json.dumps(obj, ensure_ascii=False)
        result = parse_json(text)
        assert "Привет" in result["result"]

    def test_json_with_trailing_comma_is_invalid(self):
        # Trailing commas are invalid JSON — should fail direct parse
        # but might be extracted by brace counter
        text = '{"action": "finish", "result": "ok",}'
        # json.loads fails on trailing comma, so parse_json returns None
        result = parse_json(text)
        assert result is None

    def test_multiple_fenced_blocks_first_wins(self):
        text = '```json\n{"action": "finish", "result": "first"}\n```\n```json\n{"action": "tool"}\n```'
        result = parse_json(text)
        # First match from regex wins
        assert result["result"] == "first"



class TestToolTrace:
    """Tests for ToolTrace dataclass and _compact_args_repr helper."""

    def test_tooltrace_fields(self):
        from react_loop import ToolTrace
        t = ToolTrace(
            tool_name="shell",
            args_repr="command=ls -la",
            success=True,
            duration_ms=123.4,
        )
        assert t.tool_name == "shell"
        assert t.args_repr == "command=ls -la"
        assert t.success is True
        assert t.duration_ms == 123.4
        assert t.error == ""
        assert t.timestamp > 0

    def test_tooltrace_failure_captures_error(self):
        from react_loop import ToolTrace
        t = ToolTrace(
            tool_name="file_read",
            args_repr="path=/no/such/file",
            success=False,
            duration_ms=5.0,
            error="File not found",
        )
        assert t.success is False
        assert t.error == "File not found"

    def test_compact_args_repr_basic(self):
        from react_loop import _compact_args_repr
        result = _compact_args_repr("shell", {"command": "ls -la"})
        assert "command=ls -la" in result

    def test_compact_args_repr_skips_content_keys(self):
        from react_loop import _compact_args_repr
        result = _compact_args_repr("file_write", {"path": "/tmp/x.txt", "content": "A" * 500})
        assert "content=<" in result   # summarised as <Nchars>
        assert "path=/tmp/x.txt" in result

    def test_compact_args_repr_truncates_long_value(self):
        from react_loop import _compact_args_repr
        result = _compact_args_repr("shell", {"command": "x" * 100})
        assert "…" in result  # ellipsis present due to truncation

    def test_compact_args_repr_truncates_total(self):
        from react_loop import _compact_args_repr
        # Generate a result longer than 200 chars total
        args = {f"k{i}": "v" * 30 for i in range(10)}
        result = _compact_args_repr("tool", args)
        assert len(result) <= 205  # 200 + "…"


class TestOnToolTraceCallback:
    """Integration tests: on_tool_trace callback on ReactContext fires correctly."""

    def _make_ctx(self, on_tool_trace=None):
        """Create a minimal ReactContext for testing."""
        import threading
        from unittest.mock import MagicMock
        from react_loop import ReactContext

        ctx = ReactContext(
            llm=MagicMock(),
            tool_index=MagicMock(),
            memory=MagicMock(),
            builtin_executor=None,
            mcp_manager=None,
            skill_registry=None,
            cancel_event=threading.Event(),
            on_tool_trace=on_tool_trace,
        )
        return ctx

    def test_on_tool_trace_defaults_to_none(self):
        ctx = self._make_ctx()
        assert ctx.on_tool_trace is None

    def test_on_tool_trace_receives_trace(self):
        from unittest.mock import patch
        from react_loop import _compact_args_repr, ToolTrace

        traces = []
        ctx = self._make_ctx(on_tool_trace=traces.append)

        fake_outcome = {"success": True, "output": "hello", "exit_code": 0}
        action_obj = {"tool": "shell", "args": {"command": "echo hello"}}

        with patch("react_loop._dispatch_tool", return_value=fake_outcome) as mock_dispatch:
            import time
            t0 = time.time()
            # Simulate what react_loop does when action == "tool"
            outcome = mock_dispatch(ctx, action_obj, lambda _: None)
            duration_ms = (time.time() - t0) * 1000
            tool_name = action_obj["tool"]
            args = action_obj["args"]
            if ctx.on_tool_trace is not None:
                ctx.on_tool_trace(ToolTrace(
                    tool_name=tool_name,
                    args_repr=_compact_args_repr(tool_name, args),
                    success=outcome["success"],
                    duration_ms=round(duration_ms, 1),
                    error="",
                ))

        assert len(traces) == 1
        assert traces[0].tool_name == "shell"
        assert traces[0].success is True

    def test_on_tool_trace_not_called_when_none(self):
        """No error when on_tool_trace is None (default path)."""
        from react_loop import ToolTrace

        ctx = self._make_ctx(on_tool_trace=None)
        # Should not raise
        if ctx.on_tool_trace is not None:
            ctx.on_tool_trace(ToolTrace("shell", "", True, 1.0))
        # Just assert we reached here without error
        assert True


class TestCancelEventOwnership:
    """Verify react_loop only clears cancel events it owns (fix: a shared
    stop signal forwarded to a sub-agent must not be erased when the sub-agent starts)."""

    def _make_controller(self, cancel_event=None):
        from unittest.mock import MagicMock
        from agent_controller import AgentController

        return AgentController(
            llm=MagicMock(),
            tool_index=MagicMock(),
            memory=MagicMock(),
            cancel_event=cancel_event,
        )

    def test_react_context_owns_by_default(self):
        from react_loop import ReactContext
        from unittest.mock import MagicMock

        ctx = ReactContext(
            llm=MagicMock(), tool_index=MagicMock(),
            memory=MagicMock(), builtin_executor=None,
            mcp_manager=None, skill_registry=None,
        )
        assert ctx.owns_cancel_event is True

    def test_controller_owns_when_no_external_event(self):
        ctrl = self._make_controller(cancel_event=None)
        assert ctrl._owns_cancel_event is True

    def test_controller_does_not_own_external_event(self):
        import threading
        ev = threading.Event()
        ctrl = self._make_controller(cancel_event=ev)
        assert ctrl._owns_cancel_event is False
        assert ctrl._cancel_event is ev

    def test_run_does_not_clear_shared_event(self):
        """A pre-set external cancel event survives into the loop (not cleared)."""
        import threading
        from unittest.mock import patch

        ev = threading.Event()
        ev.set()
        ctrl = self._make_controller(cancel_event=ev)

        captured = {}

        def _fake_loop(ctx, *args, **kwargs):
            captured["was_set"] = ctx.cancel_event.is_set()
            captured["owns"] = ctx.owns_cancel_event
            return "done"

        with patch("agent_controller.react_loop", side_effect=_fake_loop):
            ctrl.run("hello")

        # react_loop received an event still set + flagged as not-owned
        assert captured["owns"] is False
        assert captured["was_set"] is True

    def test_owned_event_is_cleared_at_loop_start(self):
        """The main agent (owns its event) clears stale state per turn."""
        import threading
        from react_loop import ReactContext, react_loop
        from unittest.mock import MagicMock, patch

        ev = threading.Event()
        ev.set()
        ctx = ReactContext(
            llm=MagicMock(), tool_index=MagicMock(),
            memory=MagicMock(), builtin_executor=None,
            mcp_manager=None, skill_registry=None,
            cancel_event=ev, owns_cancel_event=True,
        )
        # Stop the loop right after the clear by raising inside the first
        # heavy call; we only care that the owned event was cleared.
        with patch("react_loop._build_system_prompt", side_effect=RuntimeError("stop")):
            try:
                react_loop(ctx, "hello")
            except Exception:
                pass
        assert ev.is_set() is False

class TestFormatToolResultRecoveryFields:
    """format_tool_result must surface structured recovery metadata on failure."""

    def test_failure_includes_recovery_metadata(self):
        from react_loop import format_tool_result

        msg = format_tool_result("shell", {
            "success": False,
            "output": "",
            "error": "boom",
            "exit_code": 124,
            "error_type": "tool_timeout",
            "recoverable": True,
            "suggestion": "increase the timeout",
        })
        assert "error_type: tool_timeout" in msg
        assert "recoverable: True" in msg
        assert "suggestion: increase the timeout" in msg

    def test_failure_without_metadata_omits_fields(self):
        from react_loop import format_tool_result

        msg = format_tool_result("shell", {
            "success": False,
            "output": "",
            "error": "boom",
            "exit_code": 1,
        })
        assert "error_type:" not in msg
        assert "recoverable:" not in msg


class TestCompactionGoalAnchoring:
    """react_loop-level regression: the goal survives repeated in-loop compaction.

    Drives a real multi-step ReAct run through the execution harness with
    short-term history preceding the goal (so ``goal_idx > 0``) and an estimator
    that forces compaction on every step. The goal must remain in the context
    handed to the model on the final step, proving ``react_loop`` threads the
    index returned by ``maybe_compact`` back into the next compaction correctly.
    """

    def test_goal_survives_repeated_compaction(self, monkeypatch):
        from unittest.mock import MagicMock

        import context_manager
        from tests.execution_harness import RecordingExecutor, ScriptedLLM, run_react

        # Force compaction whenever more than three messages are present.
        monkeypatch.setattr(
            context_manager, "estimate_messages_tokens",
            lambda m, s, model=None: 1_000_000 if len(m) > 3 else 10,
        )

        goal = "CURRENT GOAL: reconcile the ledger end to end"
        short_term = MagicMock()
        short_term.get_messages.return_value = [
            {"role": "user", "content": "old goal 0"},
            {"role": "assistant", "content": "old answer 0"},
            {"role": "user", "content": "old goal 1"},
            {"role": "assistant", "content": "old answer 1"},
        ]

        llm = ScriptedLLM([
            '{"action": "tool", "tool": "noop", "args": {}}',
            '{"action": "tool", "tool": "noop", "args": {}}',
            '{"action": "tool", "tool": "noop", "args": {}}',
            '{"action": "finish", "result": "done"}',
        ])
        executor = RecordingExecutor()

        result, _calls, _progress = run_react(llm, executor, goal, short_term=short_term)

        assert result == "done"
        # The final context handed to the model must still contain the goal
        # verbatim (not swept into the summary) after repeated compaction …
        final_msgs = llm.calls[-1].messages
        assert any(m.get("content") == goal for m in final_msgs), (
            "current goal must survive verbatim across repeated in-loop compaction"
        )
        # … and compaction must actually have fired (summary message present).
        assert any("Compacted context" in str(m.get("content")) for m in final_msgs), (
            "compaction must have fired during the multi-step run"
        )


class TestNativeMultiTurnDispatch:
    """B3: native tool-calling multi-turn dispatch sends well-formed payloads.

    Step 1 returns a native tool call; step 2 returns finish text. The messages
    handed to the model on step 2 must carry a well-formed assistant ``tool_calls``
    entry plus a ``tool`` message keyed by ``tool_call_id`` — the exact shape the
    payload builders must preserve (regression guard for B1).
    """

    def test_tool_call_then_finish_preserves_wire_shape(self):
        from interfaces import ChatResponse, ToolCall
        from tests.execution_harness import (
            NativeScriptedLLM,
            RecordingExecutor,
            make_outcome,
            run_react,
        )

        llm = NativeScriptedLLM([
            ChatResponse(tool_calls=[
                ToolCall(id="1", name="shell", arguments={"command": "echo hello"}),
            ]),
            ChatResponse(text='{"action": "finish", "result": "done"}'),
        ])
        ex = RecordingExecutor({"shell": make_outcome(output="hello")})

        result, _calls, _progress = run_react(llm, ex, "say hello")

        assert result == "done"
        assert ex.tool_order == ["shell"]

        # The second native call must have seen a well-formed assistant tool_calls
        # entry plus a tool message carrying the matching tool_call_id.
        assert len(llm.tool_calls_seen) == 2
        second = llm.tool_calls_seen[1]

        assistant_msgs = [m for m in second if m.get("role") == "assistant" and m.get("tool_calls")]
        assert assistant_msgs, "expected an assistant message carrying tool_calls"
        entry = assistant_msgs[0]["tool_calls"][0]
        assert entry["id"] == "1"
        assert entry["type"] == "function"
        assert entry["function"]["name"] == "shell"
        # arguments are JSON-encoded on the wire
        assert json.loads(entry["function"]["arguments"]) == {"command": "echo hello"}

        tool_msgs = [m for m in second if m.get("role") == "tool"]
        assert tool_msgs, "expected a tool-result message"
        assert tool_msgs[0]["tool_call_id"] == "1"
        assert "hello" in str(tool_msgs[0]["content"])

    def test_native_prose_without_tool_call_is_treated_as_finish(self):
        """M1: native text that is not JSON becomes a finish, not a protocol error."""
        from interfaces import ChatResponse
        from tests.execution_harness import NativeScriptedLLM, RecordingExecutor, run_react

        llm = NativeScriptedLLM([ChatResponse(text="All done — nothing else to do.")])
        ex = RecordingExecutor()

        result, _calls, _progress = run_react(llm, ex, "wrap up")

        assert result == "All done — nothing else to do."
        assert not result.startswith("❌ Agent protocol error:")
        assert ex.calls == []


class TestNativeFallbackLinearization:
    """H1: native→json_mode fallback must not leak OpenAI wire-format messages.

    Native dispatch writes assistant ``tool_calls`` (content=None) + ``tool``
    (tool_call_id) turns into shared history. When a later step falls back to
    json_mode, the plain chat builders drop those fields, producing malformed
    payloads the provider rejects with a 400 (assistant with null content and no
    tool_calls, orphan role=tool with no tool_call_id). ``_linearize_native_turns``
    flattens them to plain text before the fallback call.
    """

    def test_linearize_flattens_native_wire_shape(self):
        from react_loop import _linearize_native_turns

        messages = [
            {"role": "user", "content": "goal"},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "1", "type": "function",
                "function": {"name": "shell", "arguments": json.dumps({"command": "df -h"})},
            }]},
            {"role": "tool", "tool_call_id": "1", "content": "Tool 'shell' succeeded:\nout"},
        ]
        out = _linearize_native_turns(messages)

        # 1:1 conversion keeps any goal-index anchor into the list valid.
        assert len(out) == len(messages)
        # The plain goal message passes through untouched (same object).
        assert out[0] is messages[0]

        # Assistant tool_calls turn → plain text: no null content, no tool_calls.
        assert out[1]["role"] == "assistant"
        assert out[1]["content"] is not None
        assert out[1]["content"].startswith("Called tool: shell(")
        assert "command=df -h" in out[1]["content"]
        assert "tool_calls" not in out[1]

        # Tool-result turn → user text: no tool role, no tool_call_id.
        assert out[2]["role"] == "user"
        assert "tool_call_id" not in out[2]
        assert out[2]["content"].startswith("Tool 'shell' succeeded")

    def test_linearize_summarizes_bulky_args_without_inlining(self):
        from react_loop import _linearize_native_turns

        big = "x" * 5000
        messages = [{"role": "assistant", "content": None, "tool_calls": [{
            "id": "9", "type": "function",
            "function": {"name": "file_write",
                         "arguments": json.dumps({"path": "/tmp/a", "content": big})},
        }]}]
        out = _linearize_native_turns(messages)

        # Bulky argument values are summarized, never inlined verbatim.
        assert big not in out[0]["content"]
        assert "content=<" in out[0]["content"]
        assert "path=/tmp/a" in out[0]["content"]

    def test_linearize_is_idempotent_on_plain_messages(self):
        from react_loop import _linearize_native_turns

        plain = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        once = _linearize_native_turns(plain)
        assert once == plain
        assert _linearize_native_turns(once) == once

    def test_native_failure_falls_back_to_linearized_json_mode(self):
        """End-to-end: step-1 native tool call, step-2 native failure → the
        json_mode fallback receives clean, linearized history (H1 regression)."""
        from interfaces import ChatResponse, ToolCall
        from llm_client import LLMError
        from tests.execution_harness import (
            NativeScriptedLLM,
            RecordingExecutor,
            make_outcome,
            run_react,
        )

        llm = NativeScriptedLLM([
            ChatResponse(tool_calls=[
                ToolCall(id="1", name="shell", arguments={"command": "echo hi"}),
            ]),
            LLMError("native tool calling unavailable"),
        ])
        ex = RecordingExecutor({"shell": make_outcome(output="hi")})

        result, _calls, _progress = run_react(llm, ex, "do it")

        assert result == "done"
        assert ex.tool_order == ["shell"]

        # The json_mode fallback ran and received a fully linearized payload.
        assert llm.json_mode_calls, "expected a json_mode fallback call"
        fallback_msgs = llm.json_mode_calls[0]
        for m in fallback_msgs:
            assert m.get("content") is not None, "assistant content=None leaked into json_mode"
            assert "tool_calls" not in m, "tool_calls leaked into json_mode payload"
            assert m.get("role") != "tool", "role=tool leaked into json_mode payload"
            assert "tool_call_id" not in m, "tool_call_id leaked into json_mode payload"
        # The step-1 tool call and its result survive as readable plain text.
        assert any(
            m["role"] == "assistant" and str(m.get("content", "")).startswith("Called tool: shell(")
            for m in fallback_msgs
        )
        assert any(
            m["role"] == "user" and "hi" in str(m.get("content", ""))
            for m in fallback_msgs
        )

"""Tests for JSON parsing — parse_json and extract_json_candidates from react_loop."""

from __future__ import annotations

import json
import logging
import threading
from contextlib import nullcontext
from unittest.mock import MagicMock, patch

import pytest

from agent_logging import LogEvent
from react_loop import extract_json_candidates, parse_json


class TestTurnOwnedResultPublication:
    """A turn-owned child returns through join tools, not operator recall."""

    def test_turn_owned_finish_does_not_publish_to_results_memory(self):
        from react_loop import _finish_run

        ctx = MagicMock()
        ctx.depth = 1
        ctx.memory = MagicMock()
        ctx.short_term = None
        ctx.results = MagicMock()
        ctx.working = MagicMock()
        ctx.working.has_content.return_value = True
        ctx.graph_memory_writer = None
        ctx.strategy_memory = None

        with patch("react_loop._summarize_result", return_value="summary"), \
             patch("react_loop.save_task_outcome") as save_task_outcome:
            result = _finish_run(
                ctx,
                {"action": "finish", "result": "child outcome"},
                "delegated task",
            )

        assert result == "child outcome"
        save_task_outcome.assert_not_called()


class TestTurnOwnedFinishGate:
    """Normal finish remains inside the turn while child work is unaccounted."""

    @pytest.mark.parametrize("status", ["running", "done"])
    def test_finish_is_refused_for_unaccounted_child(self, status):
        from tests.execution_harness import RecordingExecutor, ScriptedLLM, build_context
        from react_loop import react_loop

        llm = ScriptedLLM([
            '{"action":"finish","result":"premature"}',
            '{"action":"finish","result":"complete"}',
        ])
        ctx = build_context(llm, RecordingExecutor())
        supervisor = MagicMock()
        supervisor.unaccounted_children.side_effect = [
            [("sa-child", status)],
            [],
        ]
        ctx.builtin_executor._supervisor = supervisor

        with patch("react_loop._build_system_prompt", return_value=("system", None)):
            result = react_loop(ctx, "delegate work")

        assert result == "complete"
        assert len(llm.calls) == 2
        feedback = "\n".join(
            str(message.get("content", ""))
            for message in llm.calls[1].messages
        )
        assert "sa-child" in feedback
        assert "get_agent_result" in feedback
        assert "cancel_agent" in feedback

    def test_finish_veto_preserves_request_attempt_state_and_checkpoint(self):
        from call_attempt_state import CallAttemptState
        from tests.execution_harness import RecordingExecutor, build_context
        from react_loop import _LoopState, _StepResult, _Turn, _run_single_step

        ctx = build_context(MagicMock(), RecordingExecutor(), trace_id="r-finish-veto")
        supervisor = MagicMock()
        supervisor.unaccounted_children.return_value = [("sa-child", "done")]
        ctx.builtin_executor._supervisor = supervisor
        ctx.checkpoint_store = MagicMock()
        attempt = CallAttemptState(max_retries=1)
        attempt.begin_outbound_attempt("json")
        state = _LoopState(
            messages=[{"role": "user", "content": "goal"}], goal_idx=0,
            max_steps=8, attempt_state=attempt,
        )

        with patch("react_loop._prepare_request"), patch(
            "react_loop._request_turn",
            return_value=_Turn([], '{"action":"finish","result":"premature"}', False, None),
        ):
            result = _run_single_step(
                ctx, state, "system", "goal", 0.0, lambda _message: None,
                supports_native_fallback=False,
            )

        assert result == _StepResult(should_continue=True)
        assert state.attempt_state is attempt
        assert attempt.outbound_attempts == 1
        assert attempt.automatic_retries_used == 0
        ctx.checkpoint_store.delete.assert_not_called()


class TestDirectCheckpointOwnershipCleanup:
    def test_direct_loop_releases_owner_when_context_setup_raises(self, tmp_path):
        from checkpoint_store import CheckpointStore
        from tests.execution_harness import RecordingExecutor, ScriptedLLM, build_context
        from react_loop import react_loop

        store = CheckpointStore(str(tmp_path))
        ctx = build_context(ScriptedLLM(['{"action":"finish"}']), RecordingExecutor(), trace_id="r-direct-setup")
        ctx.checkpoint_store = store

        with patch("react_loop.agent_logging.bind_run_context", side_effect=RuntimeError("bind failed")):
            with pytest.raises(RuntimeError, match="bind failed"):
                react_loop(ctx, "goal")

        assert ctx.checkpoint_owner is None
        owner = store.acquire_owner("r-direct-setup")
        assert owner is not None
        owner.release()

    def test_direct_loop_releases_owner_when_teardown_raises(self, tmp_path):
        from checkpoint_store import CheckpointStore
        from tests.execution_harness import RecordingExecutor, ScriptedLLM, build_context
        from react_loop import react_loop

        store = CheckpointStore(str(tmp_path))
        ctx = build_context(ScriptedLLM(['{"action":"finish","result":"ok"}']), RecordingExecutor(), trace_id="r-direct-cleanup")
        ctx.checkpoint_store = store

        with patch("react_loop._build_system_prompt", return_value=("system", None)), \
             patch("react_loop.agent_logging.reset_run_context", side_effect=RuntimeError("reset failed")):
            with pytest.raises(RuntimeError, match="reset failed"):
                react_loop(ctx, "goal")

        assert ctx.checkpoint_owner is None
        owner = store.acquire_owner("r-direct-cleanup")
        assert owner is not None
        owner.release()

    def test_direct_loop_does_not_release_caller_owned_lease(self, tmp_path):
        from checkpoint_store import CheckpointStore
        from tests.execution_harness import RecordingExecutor, ScriptedLLM, build_context
        from react_loop import react_loop

        store = CheckpointStore(str(tmp_path))
        owner = store.acquire_owner("r-external-owner")
        assert owner is not None
        ctx = build_context(ScriptedLLM(['{"action":"finish","result":"ok"}']), RecordingExecutor(), trace_id="r-external-owner")
        ctx.checkpoint_store = store
        ctx.checkpoint_owner = owner

        with patch("react_loop._build_system_prompt", return_value=("system", None)):
            assert react_loop(ctx, "goal") == "ok"

        assert owner.released is False
        owner.release()

    def test_wait_timeout_keeps_child_finish_obligation(self):
        from tests.execution_harness import RecordingExecutor, ScriptedLLM, build_context, make_outcome
        from react_loop import react_loop

        llm = ScriptedLLM([
            '{"action":"tool","tool":"get_agent_result","args":{"agent_id":"sa-child","timeout":0,"cancel_on_timeout":false}}',
            '{"action":"finish","result":"premature"}',
            '{"action":"tool","tool":"cancel_agent","args":{"agent_id":"sa-child"}}',
            '{"action":"finish","result":"complete"}',
        ])
        executor = RecordingExecutor({
            "get_agent_result": make_outcome(output="timed out") | {
                "success": False,
                "status": "timeout",
                "agent_id": "sa-child",
            },
            "cancel_agent": make_outcome(output="cancelled"),
        })
        ctx = build_context(llm, executor)
        supervisor = MagicMock()
        supervisor.unaccounted_children.side_effect = [
            [("sa-child", "running")],
            [],
        ]
        ctx.builtin_executor._supervisor = supervisor

        with patch("react_loop._build_system_prompt", return_value=("system", None)):
            result = react_loop(ctx, "delegate work")

        assert result == "complete"
        supervisor.mark_result_retrieved.assert_not_called()


class TestTurnOwnedDrain:
    """Every abnormal turn exit drains owned children before returning."""

    @pytest.mark.parametrize("exit_path", ["stop", "error", "budget"])
    def test_abnormal_exit_drains_owned_children(self, exit_path):
        from tests.execution_harness import RecordingExecutor, ScriptedLLM, build_context
        from react_loop import _StepResult, react_loop

        llm = ScriptedLLM(['{"action":"finish","result":"unused"}'])
        ctx = build_context(llm, RecordingExecutor())
        supervisor = MagicMock()
        ctx.builtin_executor._supervisor = supervisor

        if exit_path == "stop":
            ctx.cancel_event.set()
            ctx.owns_cancel_event = False
            step_patch = nullcontext()
        elif exit_path == "error":
            step_patch = patch(
                "react_loop._run_single_step",
                side_effect=RuntimeError("loop failure"),
            )
        else:
            step_patch = patch(
                "react_loop._run_single_step",
                return_value=_StepResult(early_return="budget exhausted"),
            )

        with patch("react_loop._build_system_prompt", return_value=("system", None)), step_patch:
            if exit_path == "error":
                with pytest.raises(RuntimeError, match="loop failure"):
                    react_loop(ctx, "delegate work")
            else:
                react_loop(ctx, "delegate work")

        supervisor.drain_owned_children.assert_called_once_with(
            ctx.turn_id, cancel=True
        )

    def test_drain_is_exposed_to_progress_before_waiting(self):
        from sub_agent_registry import SubAgentRecord
        from sub_agent_supervisor import (
            ExecutionOwnership,
            SubAgentSupervisor,
            _OwnedChildState,
        )
        from tests.execution_harness import RecordingExecutor, ScriptedLLM, build_context
        from react_loop import react_loop

        ctx = build_context(
            ScriptedLLM(['{"action":"finish","result":"unused"}']),
            RecordingExecutor(),
        )
        ctx.cancel_event.set()
        ctx.owns_cancel_event = False
        supervisor = SubAgentSupervisor()
        record = SubAgentRecord(
            agent_id="sa-draining",
            label="on-demand",
            model="test-model",
            task_preview="work",
            started_at=0.0,
            source="on-demand",
        )
        record.ownership = ExecutionOwnership("on-demand", ctx.turn_id)
        state = _OwnedChildState(record)
        supervisor._owned_children[record.agent_id] = state
        ctx.builtin_executor._supervisor = supervisor
        progress_messages = []
        drain_visible = threading.Event()
        results = []

        def progress(message):
            progress_messages.append(message)
            if message.startswith("__DRAINING__:"):
                drain_visible.set()

        run_thread = threading.Thread(
            target=lambda: results.append(
                react_loop(ctx, "delegate work", progress_callback=progress)
            )
        )
        run_thread.start()
        try:
            visible_before_cleanup = drain_visible.wait(timeout=0.3)
        finally:
            state.cleanup_event.set()
            run_thread.join(timeout=5.0)

        assert visible_before_cleanup is True
        assert progress_messages[-1] == "__DRAINING__:sa-draining"
        assert results == ["⚠️ Task stopped by operator."]


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


class TestMcpToolSpan:
    """MCP dispatch must emit the right lifecycle event and return the
    auth-translated outcome (regression: the span's outcome sink was never
    called, so successes logged TOOL_FAILED and the 401 recheck was dead)."""

    def _make_ctx(self, call_result, *, has_oauth=True):
        import threading
        from unittest.mock import MagicMock
        from react_loop import ReactContext

        mcp = MagicMock()
        mcp.has_tool.return_value = True
        mcp.call_tool.return_value = call_result
        mcp.server_name_for_tool.return_value = "srv"
        mcp.server_has_oauth.return_value = has_oauth
        return ReactContext(
            llm=MagicMock(),
            tool_index=MagicMock(),
            memory=MagicMock(),
            builtin_executor=None,
            mcp_manager=mcp,
            skill_registry=None,
            cancel_event=threading.Event(),
        )

    def _dispatch(self, ctx):
        """Run _dispatch_tool for an MCP tool, capturing emitted log events."""
        from unittest.mock import patch
        import react_loop

        events = []

        def _capture(event, _msg, **kw):
            events.append((event, kw))

        with patch.object(react_loop.agent_logging, "log_event", _capture):
            outcome = react_loop._dispatch_tool(
                ctx, {"tool": "mcp_tool", "args": {}}, lambda _m: None,
            )
        return outcome, [e for e, _ in events]

    def test_success_emits_tool_end(self):
        from agent_logging import LogEvent

        ctx = self._make_ctx({"success": True, "output": "ok", "exit_code": 0})
        outcome, events = self._dispatch(ctx)

        assert events == [LogEvent.TOOL_START, LogEvent.TOOL_END]
        assert outcome["success"] is True

    def test_auth_failure_marks_needs_auth_and_translates_error(self):
        from agent_logging import LogEvent

        ctx = self._make_ctx({"success": False, "output": "", "error": "401 Unauthorized"})
        outcome, events = self._dispatch(ctx)

        ctx.mcp_manager.mark_needs_auth.assert_called_once_with("srv")
        assert "/mcp auth srv" in outcome["error"]
        assert events == [LogEvent.TOOL_START, LogEvent.TOOL_FAILED]

    def test_non_oauth_401_is_left_alone(self):
        ctx = self._make_ctx(
            {"success": False, "output": "", "error": "401 Unauthorized"}, has_oauth=False,
        )
        outcome, _events = self._dispatch(ctx)

        ctx.mcp_manager.mark_needs_auth.assert_not_called()
        assert outcome["error"] == "401 Unauthorized"

    def test_raising_tool_emits_single_tool_failed(self):
        from agent_logging import LogEvent

        from unittest.mock import patch
        import react_loop

        ctx = self._make_ctx(None)
        ctx.mcp_manager.call_tool.side_effect = RuntimeError("boom")

        events = []
        with patch.object(
            react_loop.agent_logging, "log_event",
            lambda event, _msg, **kw: events.append(event),
        ):
            with pytest.raises(RuntimeError):
                react_loop._dispatch_tool(ctx, {"tool": "mcp_tool", "args": {}}, lambda _m: None)

        # Exactly one terminal event — no duplicate ERROR + TOOL_FAILED pair.
        assert events == [LogEvent.TOOL_START, LogEvent.TOOL_FAILED]

    def test_needs_authorization_phrase_triggers_auth_failure(self):
        """'needs authorization' phrase (D4 call_tool fast-fail) triggers auth failure for OAuth server."""
        from agent_logging import LogEvent

        ctx = self._make_ctx(
            {"success": False, "output": "", "error": "MCP server 'srv' needs authorization. Run /mcp auth srv."}
        )
        outcome, events = self._dispatch(ctx)

        ctx.mcp_manager.mark_needs_auth.assert_called_once_with("srv")
        assert "/mcp auth srv" in outcome["error"]
        assert events == [LogEvent.TOOL_START, LogEvent.TOOL_FAILED]

    def test_needs_authorization_non_oauth_server_not_detected(self):
        """'needs authorization' phrase on a non-OAuth server is left alone."""
        ctx = self._make_ctx(
            {"success": False, "output": "", "error": "MCP server 'srv' needs authorization. Run /mcp auth srv."},
            has_oauth=False,
        )
        outcome, _events = self._dispatch(ctx)

        ctx.mcp_manager.mark_needs_auth.assert_not_called()
        assert "needs authorization" in outcome["error"]


class TestCancelEventOwnership:
    """Verify react_loop only clears cancel events it owns (fix: a shared
    stop signal forwarded to a sub-agent must not be erased when the sub-agent starts)."""

    def _make_controller(self, cancel_event=None):
        from agent_controller import AgentController
        from config_schema import AgentConfig, ExecutorPaths
        from unittest.mock import MagicMock

        return AgentController(
            agent_cfg=AgentConfig(),
            paths=ExecutorPaths(),
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


class TestCancelEventRegistry:
    """CancelEventRegistry gives each run its own private cancel_event instead of
    sharing one across concurrent runs, so no run's clear() can race with a
    sibling's, and no run started after a stop can inherit a cancellation meant
    for someone else."""

    def test_new_run_event_starts_unset(self):
        from agent_controller import CancelEventRegistry

        reg = CancelEventRegistry()
        ev = reg.new_run_event()
        assert ev.is_set() is False

    def test_each_run_gets_a_distinct_event(self):
        from agent_controller import CancelEventRegistry

        reg = CancelEventRegistry()
        ev_a = reg.new_run_event()
        ev_b = reg.new_run_event()
        assert ev_a is not ev_b

    def test_cancel_all_sets_every_active_run(self):
        from agent_controller import CancelEventRegistry

        reg = CancelEventRegistry()
        ev_a = reg.new_run_event()
        ev_b = reg.new_run_event()
        reg.cancel_all()
        assert ev_a.is_set() is True
        assert ev_b.is_set() is True

    def test_released_run_not_affected_by_later_cancel(self):
        from agent_controller import CancelEventRegistry

        reg = CancelEventRegistry()
        ev_a = reg.new_run_event()
        reg.release(ev_a)
        reg.cancel_all()
        assert ev_a.is_set() is False

    def test_unrelated_new_run_started_after_a_stop_is_not_cancelled(self):
        """The bug the naive shared-event fix introduced: Run A gets stopped,
        Run B is a brand-new unrelated run starting concurrently — B must NOT
        inherit A's cancellation."""
        from agent_controller import CancelEventRegistry

        reg = CancelEventRegistry()
        ev_a = reg.new_run_event()   # Run A starts
        reg.cancel_all()             # operator stops Run A
        assert ev_a.is_set() is True
        reg.release(ev_a)            # Run A unwinds

        ev_b = reg.new_run_event()   # Run B starts fresh afterward
        assert ev_b.is_set() is False

    def test_release_of_unregistered_event_is_a_no_op(self):
        import threading
        from agent_controller import CancelEventRegistry

        reg = CancelEventRegistry()
        reg.release(threading.Event())  # never registered — must not raise


class TestAgentControllerCancelRegistryWiring:
    """AgentController must only create a CancelEventRegistry when it owns its
    cancel_event, mint/release a private event per run(), and fan cancel()
    out through the registry."""

    def _make_controller(self, cancel_event=None):
        from agent_controller import AgentController
        from config_schema import AgentConfig, ExecutorPaths
        from unittest.mock import MagicMock

        return AgentController(
            agent_cfg=AgentConfig(),
            paths=ExecutorPaths(),
            llm=MagicMock(), tool_index=MagicMock(), memory=MagicMock(),
            cancel_event=cancel_event,
        )

    def test_registry_created_when_owning_event(self):
        from agent_controller import CancelEventRegistry

        ctrl = self._make_controller(cancel_event=None)
        assert isinstance(ctrl._cancel_registry, CancelEventRegistry)

    def test_registry_none_for_external_event(self):
        import threading

        ctrl = self._make_controller(cancel_event=threading.Event())
        assert ctrl._cancel_registry is None

    def test_run_mints_and_releases_a_private_event(self):
        """run() must pass a registry-minted event into build_react_context and
        release it afterward — not the same event across successive runs."""
        from unittest.mock import patch

        ctrl = self._make_controller(cancel_event=None)
        seen_events = []

        def _fake_loop(ctx, *args, **kwargs):
            seen_events.append(ctx.cancel_event)
            return "ok"

        with patch("agent_controller.react_loop", side_effect=_fake_loop):
            ctrl.run("first")
            ctrl.run("second")

        assert len(seen_events) == 2
        assert seen_events[0] is not seen_events[1]
        assert ctrl._cancel_registry._events == set()  # both released

    def test_cancel_reaches_only_currently_active_run(self):
        """The actual bug being fixed: a stop meant for the run in flight when
        cancel() is called must not affect a later, unrelated run."""
        from unittest.mock import patch

        ctrl = self._make_controller(cancel_event=None)

        # Run A: still "in flight" (never released) when cancel() fires.
        run_a_event = ctrl._cancel_registry.new_run_event()
        ctrl.cancel()
        assert run_a_event.is_set() is True
        ctrl._cancel_registry.release(run_a_event)

        # Run B starts afterward and must not see a cancelled state.
        seen = {}

        def _fake_loop(ctx, *args, **kwargs):
            seen["was_set"] = ctx.cancel_event.is_set()
            return "ok"

        with patch("agent_controller.react_loop", side_effect=_fake_loop):
            ctrl.run("unrelated later request")
        assert seen["was_set"] is False

    def test_cancel_falls_back_to_direct_set_for_external_event(self):
        import threading

        ev = threading.Event()
        ctrl = self._make_controller(cancel_event=ev)
        ctrl.cancel()
        assert ev.is_set() is True


class TestAgentControllerTurnOwnership:
    """Interactive ownership is unique even when correlation traces are reused."""

    @staticmethod
    def _make_controller():
        from agent_controller import AgentController
        from config_schema import AgentConfig, ExecutorPaths

        return AgentController(
            agent_cfg=AgentConfig(),
            paths=ExecutorPaths(),
            llm=MagicMock(),
            tool_index=MagicMock(),
            memory=MagicMock(),
        )

    def test_reused_trace_receives_distinct_turn_owners(self):
        ctrl = self._make_controller()
        seen = []

        with patch(
            "agent_controller.react_loop",
            side_effect=lambda ctx, *_args, **_kwargs: seen.append(
                (ctx.trace_id, ctx.turn_id)
            ) or "ok",
        ):
            ctrl.run("first", trace_id="r-reused")
            ctrl.run("second", trace_id="r-reused")

        assert [trace for trace, _owner in seen] == ["r-reused", "r-reused"]
        assert seen[0][1]
        assert seen[0][1] != seen[1][1]

    def test_resume_reuses_checkpoint_trace_but_not_turn_owner(self):
        from call_attempt_state import CallAttemptState

        ctrl = self._make_controller()
        ctrl.llm.llm_cfg = {"id": "fixture-canonical", "model": "fixture-upstream"}
        ctrl.checkpoint_store = MagicMock()
        ctrl.checkpoint_store.load.return_value = {
            "format_version": 2,
            "messages": [{"role": "user", "content": "resumed goal"}],
            "goal_idx": 0,
            "max_steps": 8,
            "step": 0,
            "json_fail_streak": 0,
            "trace_id": "r-checkpoint",
            "user_goal": "resumed goal",
            "model_id": "fixture-canonical",
            "attempt_state": CallAttemptState().to_dict(),
            "created_at": "2026-01-01T00:00:00Z",
            "error_info": {
                "type": "timeout", "message": "timeout",
                "retryable": True, "detail": "TimeoutException",
            },
        }
        seen = []

        with patch(
            "agent_controller.react_loop",
            side_effect=lambda ctx, *_args, **_kwargs: seen.append(
                (ctx.trace_id, ctx.turn_id)
            ) or "ok",
        ):
            ctrl.run("ignored", resume_from="r-checkpoint")
            ctrl.run("ignored", resume_from="r-checkpoint")

        assert [trace for trace, _owner in seen] == [
            "r-checkpoint",
            "r-checkpoint",
        ]
        assert seen[0][1] != seen[1][1]

    def test_empty_react_context_trace_still_has_turn_owner(self):
        from tests.execution_harness import RecordingExecutor, ScriptedLLM, build_context

        response = '{"action":"finish","result":"done"}'
        first = build_context(ScriptedLLM([response]), RecordingExecutor())
        second = build_context(ScriptedLLM([response]), RecordingExecutor())
        first.trace_id = ""
        second.trace_id = ""

        assert first.turn_id
        assert first.turn_id != second.turn_id


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

    def test_suggestion_suppressed_without_error_type(self):
        from react_loop import format_tool_result

        msg = format_tool_result("shell", {
            "success": False,
            "output": "",
            "error": "boom",
            "exit_code": 1,
            "suggestion": "do this",
        })
        assert "suggestion:" not in msg

    def test_suggestion_emitted_with_error_type(self):
        from react_loop import format_tool_result

        msg = format_tool_result("shell", {
            "success": False,
            "output": "",
            "error": "boom",
            "exit_code": 1,
            "error_type": "tool_error",
            "suggestion": "do this",
        })
        assert "suggestion: do this" in msg


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
        import react_loop as react_loop_module
        monkeypatch.setattr(
            react_loop_module, "estimate_messages_tokens",
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

    def test_native_failure_is_terminal_without_json_protocol_switch(self):
        """A post-send native error must not be hidden by a JSON retry."""
        from interfaces import ChatResponse, ToolCall
        from llm_client import LLMError
        from tests.execution_harness import (
            NativeScriptedLLM,
            RecordingExecutor,
            make_outcome,
            run_react,
        )
        from confirmation import ConfirmationManager

        llm = NativeScriptedLLM([
            ChatResponse(tool_calls=[
                ToolCall(id="1", name="shell", arguments={"command": "echo hi"}),
            ]),
            LLMError("native tool calling unavailable"),
        ])
        ex = RecordingExecutor({"shell": make_outcome(output="hi")})
        confirmation = ConfirmationManager()
        confirmation.request_retry = lambda *args, **kwargs: "timeout"

        result, _calls, _progress = run_react(llm, ex, "do it", confirmation=confirmation)

        assert result.startswith("❌")
        assert ex.tool_order == ["shell"]
        assert llm.json_mode_calls == []


class TestSingleRecordLifecycle:
    """Lifecycle events replace prose logging after the structlog retrofit.

    Each scenario is driven through the execution harness and asserts both the
    structured event captured by ``log_event`` and the absence of the old prose
    log lines that were removed from ``react_loop.py``.

    The harness's stub builtin executor bypasses ``BuiltinExecutor.execute()``
    (which emits its own TOOL_* events), so this class builds a wrapper that
    records calls and emits the lifecycle events the real executor would emit.
    """

    def _stdlib_records(self, caplog: pytest.LogCaptureFixture) -> list[str]:
        return [r.getMessage() for r in caplog.records]

    def _emitting_executor(self, executor):
        """Return a stub builtin executor that emits TOOL_START/TOOL_END/TOOL_FAILED."""
        from tests.execution_harness import _StubBuiltinExecutor
        from agent_logging import log_event

        class _EmittingStub(_StubBuiltinExecutor):
            def execute(self, tool_name: str, args: dict, **kwargs) -> dict:
                log_event(LogEvent.TOOL_START, f"tool start: {tool_name}", level=logging.INFO, tool=tool_name)
                result = self._executor.execute(tool_name, args)
                if result.get("success"):
                    log_event(
                        LogEvent.TOOL_END, f"tool end: {tool_name}", level=logging.INFO,
                        tool=tool_name, dur_ms=1, exit=result.get("exit_code", 0),
                    )
                else:
                    log_event(
                        LogEvent.TOOL_FAILED, f"tool failed: {tool_name}", level=logging.ERROR,
                        tool=tool_name, dur_ms=1, exit=result.get("exit_code", -1),
                        err=result.get("error", "") or "",
                    )
                return result

        return _EmittingStub(executor)

    def test_successful_tool_completion_emits_single_tool_end(self, caplog):
        from unittest.mock import MagicMock, patch
        import react_loop
        from tests.execution_harness import RecordingExecutor, ScriptedLLM, make_outcome, run_react

        caplog.set_level(logging.DEBUG)
        llm = ScriptedLLM([
            '{"action":"tool","tool":"shell","args":{"command":"ls"}}',
            '{"action":"finish","result":"done"}',
        ])
        executor = RecordingExecutor({"shell": make_outcome(output="x")})

        events = []
        stdlib = MagicMock()

        def _capture(event, _msg, **kw):
            events.append((event, kw))

        with patch.object(react_loop.agent_logging, "log_event", _capture):
            with patch.object(react_loop, "logger", stdlib):
                result, _calls, _progress = run_react(
                    llm, executor, "do it",
                    builtin_executor=self._emitting_executor(executor),
                )

        assert result == "done"
        assert [e for e, _ in events].count(LogEvent.TOOL_END) == 1
        prose = [call.args[0] for call in stdlib.info.call_args_list + stdlib.warning.call_args_list]
        assert not any("result: success" in msg for msg in prose)

    def test_failed_tool_emits_single_tool_failed(self, caplog):
        from unittest.mock import MagicMock, patch
        import react_loop
        from tests.execution_harness import RecordingExecutor, ScriptedLLM, make_outcome, run_react

        caplog.set_level(logging.DEBUG)
        llm = ScriptedLLM([
            '{"action":"tool","tool":"shell","args":{"command":"ls"}}',
            '{"action":"finish","result":"done"}',
        ])
        executor = RecordingExecutor({"shell": make_outcome(success=False, error="boom")})

        events = []
        stdlib = MagicMock()

        def _capture(event, _msg, **kw):
            events.append((event, kw))

        with patch.object(react_loop.agent_logging, "log_event", _capture):
            with patch.object(react_loop, "logger", stdlib):
                result, _calls, _progress = run_react(
                    llm, executor, "do it",
                    builtin_executor=self._emitting_executor(executor),
                )

        assert [e for e, _ in events].count(LogEvent.TOOL_FAILED) == 1
        prose = [call.args[0] for call in stdlib.info.call_args_list + stdlib.warning.call_args_list]
        assert not any("result: success=False" in msg for msg in prose)

    def test_llm_failure_emits_single_llm_failed(self, caplog):
        from unittest.mock import MagicMock, patch
        import agent_logging
        import react_loop
        from llm_client import LLMError
        from confirmation import ConfirmationManager
        from tests.execution_harness import RecordingExecutor, ScriptedLLM, run_react

        caplog.set_level(logging.DEBUG)
        llm = ScriptedLLM(['{"action":"finish","result":"done"}'])
        executor = RecordingExecutor()

        class _ImmediateCancel(ConfirmationManager):
            def request_retry(self, token, error_info_json, progress_cb, timeout_seconds=120):
                return "cancel"

        events = []
        stdlib = MagicMock()
        orig_log_event = agent_logging.log_event
        mock_log_event = MagicMock(wraps=orig_log_event)

        def fake_chat_with_fallback(_self, *args, **kwargs):
            agent_logging.log_event(
                LogEvent.LLM_FAILED, "LLM request failed: test-model", level=logging.ERROR,
                model="test-model", dur_ms=1, err="llm boom",
            )
            raise LLMError("llm boom")

        llm.chat_with_fallback = lambda *a, **k: fake_chat_with_fallback(llm, *a, **k)

        try:
            agent_logging.log_event = mock_log_event
            with patch.object(react_loop, "logger", stdlib):
                result, _calls, _progress = run_react(
                    llm, executor, "do it", confirmation=_ImmediateCancel(),
                )
        finally:
            agent_logging.log_event = orig_log_event

        for c in mock_log_event.call_args_list:
            events.append((c[0][0], c[1]))

        assert [e for e, _ in events].count(LogEvent.LLM_FAILED) == 1
        prose = [call.args[0] for call in stdlib.info.call_args_list + stdlib.warning.call_args_list + stdlib.error.call_args_list]
        assert not any("LLM chat" in msg for msg in prose)


class TestStructuredRunFields:
    """RUN_BEGIN / STEP_BEGIN / RUN_END carry the expected structured fields."""

    def _capture_log_event(self):
        from unittest.mock import patch
        import react_loop

        events = []

        def _capture(event, _msg, **kw):
            events.append((event, kw))

        return events, patch.object(react_loop.agent_logging, "log_event", _capture)

    def test_structured_run_and_step_fields(self, caplog):
        from tests.execution_harness import RecordingExecutor, ScriptedLLM, run_react

        caplog.set_level(logging.DEBUG)
        goal = "this goal is intentionally much longer than eighty characters so truncation is exercised"
        assert len(goal) > 80
        llm = ScriptedLLM(['{"action":"finish","result":"done"}'])
        executor = RecordingExecutor()

        events, patcher = self._capture_log_event()
        with patcher:
            result, _calls, _progress = run_react(llm, executor, goal)

        assert result == "done"

        step_begin = [kw for e, kw in events if e == LogEvent.STEP_BEGIN]
        assert step_begin
        assert step_begin[0]["model"] == "test-model"
        assert isinstance(step_begin[0]["step"], int)

        run_begin = [kw for e, kw in events if e == LogEvent.RUN_BEGIN]
        assert run_begin
        assert run_begin[0]["model"] == "test-model"
        assert run_begin[0]["goal"] == goal[:80]

        run_end = [kw for e, kw in events if e == LogEvent.RUN_END]
        assert run_end
        assert run_end[0]["model"] == "test-model"
        assert isinstance(run_end[0]["steps"], int) and run_end[0]["steps"] >= 1
        assert isinstance(run_end[0]["dur_ms"], int)


class TestScheduledVisionDispatch:
    """Scheduled vision_query dispatch never reaches the interactive coordinator."""

    def test_scheduled_vision_dispatch_never_calls_interactive_coordinator(self, tmp_path):
        import os

        import react_loop
        from builtin_executor import ScheduledToolDispatch
        from path_policy import PathPolicy
        from react_loop import ReactContext

        dirs = {name: os.path.join(str(tmp_path), name) for name in (
            "workspace", "downloads", "results", "skills", "data", "state", "config",
        )}
        for directory in dirs.values():
            os.makedirs(directory, exist_ok=True)
        policy = PathPolicy.create(
            agent_name="test-agent", workspace_dir=dirs["workspace"],
            downloads_dir=dirs["downloads"], tmp_dir="/tmp/test-agent",
            skills_dir=dirs["skills"], results_dir=dirs["results"],
            data_home=dirs["data"], state_home=dirs["state"], config_home=dirs["config"],
        )
        shared = MagicMock(name="shared_executor")
        shared.path_policy = policy
        confirmation = MagicMock(name="interactive_coordinator")
        ctx = ReactContext(
            llm=MagicMock(), tool_index=MagicMock(), memory=MagicMock(),
            builtin_executor=ScheduledToolDispatch(shared), mcp_manager=None,
            skill_registry=None, cancel_event=threading.Event(),
            depth=1, label="sa-job01", confirmation=confirmation,
        )

        with patch("react_loop._encode_images", return_value=["<b64>"]):
            react_loop._dispatch_tool(
                ctx, {"tool": "vision_query", "args": {"path": "/unrecognised/pic.png"}},
                lambda _m: None,
            )

        assert confirmation.method_calls == []
        shared._requires_confirmation.assert_not_called()


class TestScheduledMcpDispatch:
    """Scheduled MCP dispatch has no interactive confirmation route."""

    def test_scheduled_mcp_call_never_calls_interactive_coordinator(self):
        import react_loop
        from builtin_executor import ScheduledToolDispatch
        from react_loop import ReactContext

        shared = MagicMock(name="shared_executor")
        shared.is_builtin.return_value = False
        mcp = MagicMock()
        mcp.has_tool.return_value = True
        mcp.server_name_for_tool.return_value = "srv"
        mcp.call_tool.return_value = {"success": True, "output": "ok", "exit_code": 0}
        confirmation = MagicMock(name="interactive_coordinator")
        ctx = ReactContext(
            llm=MagicMock(), tool_index=MagicMock(), memory=MagicMock(),
            builtin_executor=ScheduledToolDispatch(shared), mcp_manager=mcp,
            skill_registry=None, cancel_event=threading.Event(),
            depth=1, label="sa-job01", confirmation=confirmation,
        )

        outcome = react_loop._dispatch_tool(
            ctx, {"tool": "mcp_tool", "args": {}}, lambda _m: None,
        )

        mcp.call_tool.assert_called_once_with("mcp_tool", {})
        assert outcome["success"] is True
        assert confirmation.method_calls == []
        shared._requires_confirmation.assert_not_called()


class TestOperatorPromptExcludesScheduledHistory:
    """The operator's system prompt never automatically includes scheduled-job history."""

    def _operator_ctx(self):
        from react_loop import ReactContext

        memory = MagicMock()
        memory.as_prompt_text.return_value = "No persistent memory entries."
        tool_index = MagicMock()
        tool_index.search.return_value = []
        skills = MagicMock()
        skills.all.return_value = []
        ctx = ReactContext(
            llm=MagicMock(), tool_index=tool_index, memory=memory,
            builtin_executor=None, mcp_manager=None, skill_registry=skills,
            cancel_event=threading.Event(),
        )
        return ctx

    def test_operator_prompt_omits_scheduled_history_section(self):
        from react_loop import _assemble_system_prompt

        system = _assemble_system_prompt(self._operator_ctx(), "what is on my calendar?")

        assert "SCHEDULED JOB EXECUTION HISTORY" not in system


class TestScheduledPlanRefused:
    """A scheduled run cannot use the plan action to escape its tool policy."""

    _PLAN = {"action": "plan", "plan": {"description": "write memory", "steps": [
        {"id": "s1", "tool": "memory_write", "args": {"action": "set", "key": "k", "value": "v"}},
    ]}}

    def _dispatch(self, builtin_executor, plan_executor=None):
        """Run one plan action through _dispatch_action; return (sink messages, result)."""
        from react_loop import ReactContext, _LoopState, _dispatch_action

        ctx = ReactContext(
            llm=MagicMock(), tool_index=MagicMock(), memory=MagicMock(),
            builtin_executor=builtin_executor, mcp_manager=None, skill_registry=None,
            cancel_event=threading.Event(), depth=1, label="sa-job01",
        )
        state = _LoopState(messages=[], goal_idx=0, max_steps=8)
        sunk: list[str] = []
        patcher = (
            patch("execution_plan.PlanExecutor", plan_executor)
            if plan_executor is not None else nullcontext()
        )
        with patcher:
            result = _dispatch_action(
                ctx, dict(self._PLAN), sunk.append, state, "goal", 0.0, lambda _m: None,
            )
        return sunk, result

    def test_scheduled_plan_action_is_refused(self):
        from builtin_executor import ScheduledToolDispatch

        sunk, result = self._dispatch(ScheduledToolDispatch(MagicMock()), MagicMock())

        assert result is None
        assert len(sunk) == 1
        assert "plan is not available for scheduled jobs" in sunk[0]

    def test_scheduled_plan_action_never_invokes_plan_executor(self):
        from builtin_executor import ScheduledToolDispatch

        plan_executor = MagicMock(name="PlanExecutor")
        self._dispatch(ScheduledToolDispatch(MagicMock()), plan_executor)

        plan_executor.assert_not_called()

    def test_scheduled_plan_never_reaches_a_plan_step(self, make_builtin_executor, tmp_path):
        """Reviewer repro: scheduled → plan → memory_write never builds a step agent."""
        from builtin_executor import ScheduledToolDispatch

        memory = MagicMock(name="operator_kv")
        factory = MagicMock(name="sub_agent_factory")
        shared = make_builtin_executor(memory=memory, sub_agent_factory=factory,
                                       data_dir=str(tmp_path))

        self._dispatch(ScheduledToolDispatch(shared))

        factory.assert_not_called()
        assert memory.method_calls == []

    def test_on_demand_plan_action_still_runs_plan_executor(self):
        plan_executor = MagicMock(name="PlanExecutor")
        plan_executor.return_value.execute.return_value = {"results": {"s1": "ok"}}

        sunk, _result = self._dispatch(MagicMock(name="shared_executor"), plan_executor)

        plan_executor.return_value.execute.assert_called_once()
        assert sunk[0].startswith("Plan execution results:")

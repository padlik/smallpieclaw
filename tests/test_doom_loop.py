"""Doom-loop detection: three identical consecutive tool failures self-terminate.

Spec: openspec/changes/step-notifications-doom-loop/specs/doom-loop-detection/spec.md

The react loop MUST abort with a descriptive stop message when the same tool
fails with the same error three times in a row (composite key
``f"{tool_name}:{error_text[:200]}"``), and MUST reset the streak on any
successful tool call or a different failure key. Detection runs for all agent
types because it lives in the shared loop.
"""
from __future__ import annotations

from tests.execution_harness import (
    RecordingExecutor,
    ScriptedLLM,
    build_context,
    make_outcome,
    run_react,
)
from react_loop import _LoopState

try:
    from react_loop import _DOOM_LOOP_LIMIT
except ImportError:  # pre-T2 react_loop: feature tests fail, regression pins still run
    _DOOM_LOOP_LIMIT = None

FAIL_OUTCOME = make_outcome(success=False, error="Permission denied", exit_code=1)
OTHER_FAIL_OUTCOME = make_outcome(success=False, error="File not found", exit_code=1)
OK_OUTCOME = make_outcome(success=True, output="ok")

# Inert step cap: high enough that the (legacy) step gate never trips and the
# pre-T2 code terminates via the scripted finish instead of the 120s
# extension-prompt block. After gate removal react_loop ignores max_iterations.
_INERT_CAP = 10_000_000


def _tool_action_json(tool: str, **args) -> str:
    import json
    return json.dumps({"action": "tool", "tool": tool, "args": args})


def _run(llm, ex, goal: str, **overrides):
    """run_react with an inert step cap so pre-T2 runs terminate."""
    return run_react(llm, ex, goal, max_iterations=_INERT_CAP, **overrides)


_FINISH = '{"action": "finish", "result": "done"}'


class TestDoomLoopAborts:
    """Three identical consecutive (tool, error) pairs abort the run."""

    def test_three_identical_failures_abort_with_doom_message(self):
        llm = ScriptedLLM([_tool_action_json("shell", command="ls")] * 3 + [_FINISH])
        ex = RecordingExecutor({"shell": FAIL_OUTCOME})
        result, calls, _ = _run(llm, ex, "doom")
        assert "stuck" in result.lower()
        assert result.startswith("❌")
        assert len(calls) == _DOOM_LOOP_LIMIT

    def test_doom_message_mentions_tool_and_count(self):
        llm = ScriptedLLM([_tool_action_json("shell", command="ls")] * 3 + [_FINISH])
        ex = RecordingExecutor({"shell": FAIL_OUTCOME})
        result, _, _ = _run(llm, ex, "doom")
        assert "shell" in result
        assert "3" in result

    def test_doom_message_contains_error_text(self):
        llm = ScriptedLLM([_tool_action_json("file_write", path="a.txt", content="x")] * 3 + [_FINISH])
        ex = RecordingExecutor({"file_write": FAIL_OUTCOME})
        result, _, _ = _run(llm, ex, "doom")
        assert "Permission denied" in result

    def test_no_steps_after_doom_abort(self):
        """The abort is terminal: exactly 3 tool calls, no 4th dispatch."""
        llm = ScriptedLLM([_tool_action_json("shell", command="ls")] * 5 + [_FINISH])
        ex = RecordingExecutor({"shell": FAIL_OUTCOME})
        result, calls, _ = _run(llm, ex, "doom")
        assert len(calls) == 3
        assert "stuck" in result.lower()

    def test_doom_loop_limit_is_three(self):
        assert _DOOM_LOOP_LIMIT == 3


class TestDoomLoopThresholds:
    """The limit is exactly three — not two, not four."""

    def test_two_identical_failures_do_not_abort(self):
        responses = [
            _tool_action_json("file_write", path="a.txt", content="1"),
            _tool_action_json("file_write", path="a.txt", content="2"),
            '{"action": "finish", "result": "done"}',
        ]
        llm = ScriptedLLM(responses)
        ex = RecordingExecutor({"file_write": FAIL_OUTCOME})
        result, calls, _ = _run(llm, ex, "doom")
        assert result == "done"
        assert len(calls) == 2

    def test_third_identical_failure_aborts(self):
        responses = [_tool_action_json("file_write", path=f"a{i}.txt", content="x") for i in range(3)] + [_FINISH]
        llm = ScriptedLLM(responses)
        ex = RecordingExecutor({"file_write": FAIL_OUTCOME})
        result, calls, _ = _run(llm, ex, "doom")
        assert "stuck" in result.lower()
        assert len(calls) == 3

    def test_four_failures_would_abort_but_at_third(self):
        """With a hypothetical limit of 4, the abort still fires at the 3rd per spec."""
        responses = [_tool_action_json("shell", command=f"cmd{i}") for i in range(4)] + [_FINISH]
        llm = ScriptedLLM(responses)
        ex = RecordingExecutor({"shell": FAIL_OUTCOME})
        result, calls, _ = _run(llm, ex, "doom")
        assert "stuck" in result.lower()
        assert len(calls) == 3


class TestDoomLoopReset:
    """Success or a different key resets the streak."""

    def test_two_failures_then_success_does_not_abort(self):
        responses = [
            _tool_action_json("shell", command="1"),
            _tool_action_json("shell", command="2"),
            _tool_action_json("shell", command="3"),
            '{"action": "finish", "result": "done"}',
        ]
        ex = RecordingExecutor(
            {"shell": lambda args: FAIL_OUTCOME if args.get("command") in ("1", "2") else OK_OUTCOME}
        )
        llm = ScriptedLLM(responses)
        result, calls, _ = _run(llm, ex, "doom")
        assert result == "done"
        assert len(calls) == 3

    def test_success_resets_repeat_counter_and_key(self):
        responses = [
            _tool_action_json("shell", command="1"),
            _tool_action_json("shell", command="2"),
            _tool_action_json("shell", command="3"),
            _tool_action_json("shell", command="4"),
            _tool_action_json("shell", command="5"),
            '{"action": "finish", "result": "done"}',
        ]
        # fail, fail, ok, fail, fail → only 2 consecutive at the end → no abort
        ex = RecordingExecutor(
            {"shell": lambda args: FAIL_OUTCOME if args.get("command") in ("1", "2", "4", "5") else OK_OUTCOME}
        )
        llm = ScriptedLLM(responses)
        result, calls, _ = _run(llm, ex, "doom")
        assert result == "done"
        assert len(calls) == 5

    def test_different_error_resets_counter(self):
        """fail(A), fail(A), fail(B) → counter for B is 1, no abort."""
        outcomes = [FAIL_OUTCOME, FAIL_OUTCOME, OTHER_FAIL_OUTCOME]
        responses = [_tool_action_json("shell", command=str(i)) for i in range(1, 4)]
        responses.append('{"action": "finish", "result": "done"}')
        ex = RecordingExecutor(
            {"shell": lambda args: outcomes[int(args.get("command")) - 1]}
        )
        llm = ScriptedLLM(responses)
        result, calls, _ = _run(llm, ex, "doom")
        assert result == "done"
        assert len(calls) == 3


class TestDoomLoopKeyIsComposite:
    """Same error from different tools does not accumulate."""

    def test_same_error_different_tools_no_accumulation(self):
        responses = [
            _tool_action_json("file_read", path="x"),
            _tool_action_json("file_write", path="y", content="c"),
            '{"action": "finish", "result": "done"}',
        ]
        llm = ScriptedLLM(responses)
        ex = RecordingExecutor({
            "file_read": FAIL_OUTCOME,
            "file_write": FAIL_OUTCOME,
        })
        result, calls, _ = _run(llm, ex, "doom")
        assert result == "done"
        assert len(calls) == 2

    def test_alternating_tools_same_error_never_reaches_three(self):
        """file_read, file_write, file_read, file_write with the same error each —
        each tool's own streak is 1/2; per-key counting never reaches 3."""
        responses = [
            _tool_action_json("file_read", path="x"),
            _tool_action_json("file_write", path="y", content="c"),
            _tool_action_json("file_read", path="x"),
            _tool_action_json("file_write", path="y", content="c"),
            '{"action": "finish", "result": "done"}',
        ]
        llm = ScriptedLLM(responses)
        ex = RecordingExecutor({
            "file_read": FAIL_OUTCOME,
            "file_write": FAIL_OUTCOME,
        })
        result, calls, _ = _run(llm, ex, "doom")
        assert result == "done"
        assert len(calls) == 4

    def test_three_different_errors_do_not_abort(self):
        outcomes = [
            make_outcome(success=False, error=f"err-{i}", exit_code=1) for i in range(3)
        ]
        responses = [_tool_action_json("shell", command=str(i)) for i in range(3)]
        responses.append('{"action": "finish", "result": "done"}')
        llm = ScriptedLLM(responses)
        ex = RecordingExecutor(
            {"shell": lambda args: outcomes[int(args.get("command"))]}
        )
        result, calls, _ = _run(llm, ex, "doom")
        assert result == "done"
        assert len(calls) == 3

    def test_error_text_longer_than_200_chars_keyed_by_prefix(self):
        """Only the first 200 chars of the error participate in the key."""
        long_error_a = "E" * 300
        long_error_b = "E" * 200 + "DIFFERENT-TAIL"
        # Same 200-char prefix → same key → 3rd aborts even though full texts differ.
        outcomes = [make_outcome(success=False, error=long_error_a, exit_code=1)] * 2 + [
            make_outcome(success=False, error=long_error_b, exit_code=1)
        ]
        responses = [_tool_action_json("shell", command=str(i)) for i in range(3)] + [_FINISH]
        llm = ScriptedLLM(responses)
        ex = RecordingExecutor(
            {"shell": lambda args: outcomes[int(args.get("command"))]}
        )
        result, calls, _ = _run(llm, ex, "doom")
        assert "stuck" in result.lower()
        assert len(calls) == 3


class TestDoomLoopStateDefaults:
    """_LoopState carries fresh doom-loop fields and they are not persisted."""

    def test_state_defaults(self):
        state = _LoopState(messages=[], goal_idx=0, max_steps=8)
        assert state._last_tool_fail_key == ""
        assert state._tool_fail_repeat == 0
        assert state._last_notified_step == -1

    def test_doom_fields_not_in_checkpoint_payload(self):
        """T2.4: streak fields are NOT persisted; a resumed run starts fresh."""
        from react_loop import _classify_llm_error, _handle_llm_error

        store_calls: list[tuple[str, dict]] = []

        class _Store:
            def save(self, trace_id, checkpoint):
                store_calls.append((trace_id, checkpoint))

            def delete(self, trace_id):
                pass

        ctx = build_context(ScriptedLLM(['{"action": "finish"}']), RecordingExecutor())
        ctx.checkpoint_store = _Store()
        ctx.checkpoint_enabled = True
        ctx.confirmation.request_retry = lambda *a, **k: "cancel"
        state = _LoopState(
            messages=[{"role": "user", "content": "g"}], goal_idx=0,
            max_steps=8, _last_tool_fail_key="shell:boom", _tool_fail_repeat=2,
        )
        info = _classify_llm_error(Exception("boom"))
        _handle_llm_error(ctx, state, info, lambda m: None, "g")
        assert store_calls, "checkpoint must be written"
        payload = store_calls[0][1]
        assert "_last_tool_fail_key" not in payload
        assert "_tool_fail_repeat" not in payload


class TestDoomLoopScheduledOrigin:
    """Doom-loop applies to scheduled/sub-agent-style runs (structural coverage)."""

    def test_scheduled_label_run_aborts_identically(self):
        llm = ScriptedLLM([_tool_action_json("shell", command="ls")] * 3 + [_FINISH])
        ex = RecordingExecutor({"shell": FAIL_OUTCOME})
        result, calls, progress = _run(llm, ex, "doom", label="sa-1", depth=1)
        assert "stuck" in result.lower()
        assert len(calls) == 3

    def test_scheduled_origin_doom_message_is_user_readable(self):
        llm = ScriptedLLM([_tool_action_json("shell", command="ls")] * 3 + [_FINISH])
        ex = RecordingExecutor({"shell": FAIL_OUTCOME})
        result, _, _ = _run(llm, ex, "doom", label="sa-2", depth=1)
        # user-readable, not an exception or None
        assert isinstance(result, str) and result
        assert "stuck" in result.lower()
        assert "shell" in result


class TestOperatorCancelRegression:
    """T10.4: operator cancellation must still terminate the collapsed loop."""

    def test_operator_cancel_via_tool_outcome_stops_loop(self):
        """A tool outcome flagged _operator_cancelled ends the run with the
        operator-stop message (the only legitimate post-break return)."""
        cancelled_outcome = make_outcome(
            success=False, error="Operation cancelled by the operator.", exit_code=1,
        )
        cancelled_outcome["_operator_cancelled"] = True
        responses = [
            _tool_action_json("shell", command="danger"),
            '{"action": "finish", "result": "should never happen"}',
        ]
        llm = ScriptedLLM(responses)
        ex = RecordingExecutor({"shell": cancelled_outcome})
        result, calls, _ = _run(llm, ex, "task")
        assert result == "⚠️ Task stopped by operator."
        assert len(calls) == 1

    def test_cancelled_result_after_two_failing_steps_then_cancel(self):
        """The operator-stop path also works after prior (non-identical) failures:
        the loop routes through should_continue=False, not the doom abort."""
        fail_a = make_outcome(success=False, error="err-A", exit_code=1)
        cancelled_outcome = make_outcome(
            success=False, error="Operation cancelled by the operator.", exit_code=1,
        )
        cancelled_outcome["_operator_cancelled"] = True
        outcomes = [fail_a, cancelled_outcome]
        responses = [
            _tool_action_json("shell", command="0"),
            _tool_action_json("shell", command="1"),
            '{"action": "finish", "result": "never"}',
        ]
        llm = ScriptedLLM(responses)
        ex = RecordingExecutor(
            {"shell": lambda args: outcomes[int(args.get("command"))]}
        )
        result, calls, _ = _run(llm, ex, "task")
        assert result == "⚠️ Task stopped by operator."
        assert len(calls) == 2

    def test_cancel_event_set_mid_run_terminates_next_step(self):
        """A cancel_event raised during a tool call terminates the run at the
        tool-branch cancel check instead of looping forever."""
        from tests.execution_harness import build_context
        from unittest.mock import patch
        from react_loop import react_loop

        def set_cancel_after_first(args):
            ctx.cancel_event.set()
            return OK_OUTCOME

        responses = [
            _tool_action_json("shell", command="first"),
            _tool_action_json("shell", command="second-never-runs"),
            '{"action": "finish", "result": "never"}',
        ]
        llm = ScriptedLLM(responses)
        ex = RecordingExecutor({"shell": set_cancel_after_first})
        with patch("react_loop._build_system_prompt", return_value=("sys", None)):
            ctx = build_context(llm, ex, max_iterations=_INERT_CAP)
            result = react_loop(ctx, "task")
        # The tool branch observes cancel_event, flags operator_cancelled,
        # and the collapsed loop breaks to the operator-stop message.
        assert result == "⚠️ Task stopped by operator."
        assert [c.tool for c in ex.calls] == ["shell"]

    def test_cancel_event_pre_set_terminates_pre_step(self):
        """A forwarded stop (cancel_event already set, not owned) yields the
        pre-step [Cancelled] early return."""

        from tests.execution_harness import build_context
        from unittest.mock import patch
        from react_loop import react_loop

        responses = [_tool_action_json("shell", command="never-runs")]
        llm = ScriptedLLM(responses)
        ex = RecordingExecutor({"shell": OK_OUTCOME})
        with patch("react_loop._build_system_prompt", return_value=("sys", None)):
            ctx = build_context(llm, ex)
            ctx.cancel_event.set()
            # Not owning the event prevents react_loop from clearing it.
            ctx.owns_cancel_event = False
            result = react_loop(ctx, "task")
        assert result == "[Cancelled]"
        assert ex.calls == []
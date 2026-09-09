"""Milestone step notifications: fire-and-forget pings at step_notify_interval.

Spec: openspec/changes/step-notifications-doom-loop/specs/step-milestone-notifications/spec.md

The loop calls ``ctx.milestone_notify_fn(step)`` after each completed step when
``step`` is a positive multiple of ``step_notify_interval``. Sub-agent contexts
(no fn) never notify; a raising fn must not kill the run; the same step number
must not fire twice (inactivity-warning iterations do not advance state.step).
"""
from __future__ import annotations

import json

from tests.execution_harness import (
    RecordingExecutor,
    ScriptedLLM,
    make_outcome,
    run_react,
)

OK_OUTCOME = make_outcome(success=True, output="ok")

# Inert step cap: high enough that the (legacy) step gate never trips in these
# scenarios, so runs terminate via the scripted finish. Kept for the pre-T1
# codebase; after the gate removal react_loop ignores max_iterations entirely.
_INERT_CAP = 10_000_000


def _tool_action(tool: str, **args) -> str:
    return json.dumps({"action": "tool", "tool": tool, "args": args})


def _finish(result: str = "done") -> str:
    return json.dumps({"action": "finish", "result": result})


def _n_tool_steps(n: int) -> list[str]:
    """Script n shell steps + a trailing finish."""
    steps = [_tool_action("shell", command=f"echo {i}") for i in range(n)]
    steps.append(_finish())
    return steps


def _run(llm, ex, goal: str, **overrides):
    """run_react with an inert step cap so long scripts terminate."""
    return run_react(llm, ex, goal, max_iterations=_INERT_CAP, **overrides)


class TestMilestoneFiresAtInterval:
    def test_fn_called_at_steps_30_60_90(self):
        called: list[int] = []
        llm = ScriptedLLM(_n_tool_steps(90))
        ex = RecordingExecutor({"shell": OK_OUTCOME})
        result, _, _ = _run(
            llm, ex, "work", step_notify_interval=30,
            milestone_notify_fn=called.append,
        )
        assert result == "done"
        assert called == [30, 60, 90]

    def test_fn_not_called_at_step_zero(self):
        called: list[int] = []
        # 1 step total: step 1 not multiple of 30 → no call, and no step-0 ping.
        llm = ScriptedLLM([_tool_action("shell", command="ls"), _finish()])
        ex = RecordingExecutor({"shell": OK_OUTCOME})
        _run(
            llm, ex, "work", step_notify_interval=30,
            milestone_notify_fn=called.append,
        )
        assert called == []

    def test_fn_not_called_at_non_milestone_steps(self):
        called: list[int] = []
        llm = ScriptedLLM(_n_tool_steps(15))
        ex = RecordingExecutor({"shell": OK_OUTCOME})
        _run(
            llm, ex, "work", step_notify_interval=30,
            milestone_notify_fn=called.append,
        )
        assert called == []

    def test_interval_one_fires_every_step(self):
        called: list[int] = []
        llm = ScriptedLLM(_n_tool_steps(3))
        ex = RecordingExecutor({"shell": OK_OUTCOME})
        _run(
            llm, ex, "work", step_notify_interval=1,
            milestone_notify_fn=called.append,
        )
        assert called == [1, 2, 3]


class TestMilestoneDisabledPaths:
    def test_interval_zero_disables_all_notifications(self):
        called: list[int] = []
        llm = ScriptedLLM(_n_tool_steps(100))
        ex = RecordingExecutor({"shell": OK_OUTCOME})
        result, calls, _ = _run(
            llm, ex, "work", step_notify_interval=0,
            milestone_notify_fn=called.append,
        )
        assert called == []
        assert len(calls) == 100

    def test_none_fn_is_noop(self):
        # sub-agent style context: milestone_notify_fn=None (the default)
        llm = ScriptedLLM(_n_tool_steps(35))
        ex = RecordingExecutor({"shell": OK_OUTCOME})
        result, _, _ = _run(
            llm, ex, "work", step_notify_interval=30,
            milestone_notify_fn=None,
        )
        assert result == "done"

    def test_subagent_default_context_sends_no_notifications(self):
        called: list[int] = []
        llm = ScriptedLLM(_n_tool_steps(35))
        ex = RecordingExecutor({"shell": OK_OUTCOME})
        # No step_notify_interval passed → defaults to 0 (sub-agent construction)
        result, _, _ = _run(
            llm, ex, "work", depth=1, label="sa-1",
            milestone_notify_fn=called.append,
        )
        assert result == "done"
        assert called == []


class TestMilestoneScheduledOrigin:
    def test_scheduled_job_emits_no_milestone_notifications(self):
        # Spec scenario: "Scheduled job emits no milestone notifications".
        # A scheduled job runs while the config default step_notify_interval
        # is 30, but the scheduler never wires milestone_notify_fn (None by
        # construction for all sub-agent types), so the step-30 boundary is
        # crossed silently and the run continues to completion.
        llm = ScriptedLLM(_n_tool_steps(35))
        ex = RecordingExecutor({"shell": OK_OUTCOME})
        result, calls, _ = _run(
            llm, ex, "work", label="sched-1", depth=1,
            step_notify_interval=30, milestone_notify_fn=None,
        )
        assert result == "done"
        assert len(calls) == 35


class TestMilestoneExceptionGuard:
    def test_raising_runtime_error_does_not_kill_run(self):
        def boom(step: int) -> None:
            raise RuntimeError("event loop is closed")

        llm = ScriptedLLM(_n_tool_steps(35))
        ex = RecordingExecutor({"shell": OK_OUTCOME})
        result, calls, _ = _run(
            llm, ex, "work", step_notify_interval=30,
            milestone_notify_fn=boom,
        )
        assert result == "done"
        assert len(calls) == 35

    def test_raising_fn_then_healthy_fn_still_records(self):
        state = {"raise_once": True}

        def flaky(step: int) -> None:
            if state["raise_once"]:
                state["raise_once"] = False
                raise RuntimeError("transient")

        called: list[int] = []
        llm = ScriptedLLM(_n_tool_steps(60))
        ex = RecordingExecutor({"shell": OK_OUTCOME})
        _run(
            llm, ex, "work", step_notify_interval=30,
            milestone_notify_fn=lambda s: (flaky(s), called.append(s)),
        )
        # The run survives and step 60 still fires after the step-30 raise.
        assert 60 in called


class TestMilestoneDoubleFireGuard:
    def test_state_defaults_last_notified_step(self):
        from react_loop import _LoopState

        state = _LoopState(messages=[], goal_idx=0, max_steps=8)
        assert state._last_notified_step == -1

    def test_inactivity_warning_iteration_fires_once_per_step(self):
        """Full double-fire simulation: force the inactivity branch so an
        iteration completes without advancing step, and confirm the milestone
        fn fires exactly once for that step number."""
        from unittest.mock import patch

        import react_loop as rl
        from tests.execution_harness import build_context
        from react_loop import react_loop

        called: list[int] = []
        step_seen: list[int] = []

        # Three shell actions then finish:
        #  iter1: step 0→1 (milestone at interval=1 → fires 1)
        #  iter2: step 1→2 (fires 2)
        #  iter3: spy rewinds clock at step==2 → inactivity warning fires,
        #         iteration returns WITHOUT advancing step (no double fire)
        #  iter4: step 2→3 (fires 3)
        #  iter5: finish
        llm = ScriptedLLM(
            [
                _tool_action("shell", command="a"),
                _tool_action("shell", command="b"),
                _tool_action("shell", command="c"),
                _finish(),
            ]
        )
        ex = RecordingExecutor({"shell": OK_OUTCOME})

        real_run_single_step = rl._run_single_step
        rewound = [False]

        def spy(ctx, state, system, user_goal, run_start, progress, supports_native_fallback):
            step_seen.append(state.step)
            if state.step == 2 and not rewound[0]:
                rewound[0] = True
                state.last_action_time = 0.0
                state.warned_inactivity = False
            return real_run_single_step(
                ctx, state, system, user_goal, run_start, progress,
                supports_native_fallback,
            )

        with patch("react_loop._build_system_prompt", return_value=("sys", None)):
            with patch.object(rl, "_run_single_step", side_effect=spy):
                ctx = build_context(
                    llm, ex, step_notify_interval=1, milestone_notify_fn=called.append,
                )
                ctx.inactivity_warn_minutes = 1
                result = react_loop(ctx, "work")

        assert result == "done"
        # step 2 observed at the start of two iterations (one non-advancing)
        assert step_seen.count(2) == 2, "expected a non-advancing iteration at step 2"
        # each step number fired exactly once despite the repeated observation
        assert called == [1, 2, 3]


class TestMilestoneNonBlocking:
    def test_loop_does_not_await_fn_result(self):
        """The fn's return value is discarded; loop continues immediately."""
        calls: list[int] = []

        def slow_fn(step: int) -> str:
            calls.append(step)
            return "ignored-future"

        llm = ScriptedLLM(_n_tool_steps(31))
        ex = RecordingExecutor({"shell": OK_OUTCOME})
        result, n_calls, _ = _run(
            llm, ex, "work", step_notify_interval=30,
            milestone_notify_fn=slow_fn,
        )
        assert result == "done"
        assert calls == [30]
        assert len(n_calls) == 31
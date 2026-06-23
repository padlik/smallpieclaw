"""Regression tests for AgentController.run() primary-model restore.

Verifies that a transient fallback during a run does not permanently demote
the main interactive model to the fallback for subsequent requests.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def _make_llm(initial_idx: int = 0) -> MagicMock:
    llm = MagicMock()
    llm._active_idx = initial_idx
    return llm


def _make_controller(llm):
    from agent_controller import AgentController

    return AgentController(
        llm=llm,
        tool_index=MagicMock(),
        executor=MagicMock(),
        creator=MagicMock(),
        memory=MagicMock(),
    )


class TestAgentControllerModelRestore:
    """AgentController.run() must restore the primary model on completion."""

    def test_restores_primary_model_on_success(self):
        """_active_idx is reset to the original value when react_loop succeeds."""
        llm = _make_llm(initial_idx=0)
        ctrl = _make_controller(llm)

        def fake_react_loop(ctx, goal, cb, images):
            # Simulate a fallback: model switched to index 1 mid-run
            ctx.llm._active_idx = 1
            return "done"

        with patch("agent_controller.react_loop", side_effect=fake_react_loop):
            result = ctrl.run("do something")

        assert result == "done"
        assert llm._active_idx == 0, (
            "Primary model index must be restored after a successful run"
        )

    def test_restores_primary_model_on_exception(self):
        """_active_idx is reset even when react_loop raises an exception."""
        llm = _make_llm(initial_idx=0)
        ctrl = _make_controller(llm)

        def failing_react_loop(ctx, goal, cb, images):
            ctx.llm._active_idx = 2  # fallback happened before crash
            raise RuntimeError("simulated crash")

        with patch("agent_controller.react_loop", side_effect=failing_react_loop):
            try:
                ctrl.run("do something")
            except RuntimeError:
                pass

        assert llm._active_idx == 0, (
            "Primary model index must be restored even after react_loop raises"
        )

    def test_restores_primary_model_on_cancellation(self):
        """_active_idx is reset when the run is cancelled (returns early)."""
        llm = _make_llm(initial_idx=0)
        ctrl = _make_controller(llm)

        def cancelling_react_loop(ctx, goal, cb, images):
            ctx.llm._active_idx = 1
            return "[Cancelled]"

        with patch("agent_controller.react_loop", side_effect=cancelling_react_loop):
            result = ctrl.run("do something")

        assert result == "[Cancelled]"
        assert llm._active_idx == 0, (
            "Primary model index must be restored after a cancelled run"
        )

    def test_does_not_change_primary_when_no_fallback_occurs(self):
        """_active_idx stays at 0 throughout when no fallback is triggered."""
        llm = _make_llm(initial_idx=0)
        ctrl = _make_controller(llm)

        def normal_react_loop(ctx, goal, cb, images):
            # No fallback — _active_idx never changes
            return "answer"

        with patch("agent_controller.react_loop", side_effect=normal_react_loop):
            ctrl.run("simple question")

        assert llm._active_idx == 0

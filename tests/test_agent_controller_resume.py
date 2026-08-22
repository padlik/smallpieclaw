"""Tests for AgentController resume-from-checkpoint wiring."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from agent_controller import AgentController
from checkpoint_store import CheckpointStore


def test_run_with_resume_from_loads_checkpoint(tmp_path):
    """Verify that run(resume_from=...) loads checkpoint and passes initial_state."""
    store = CheckpointStore(str(tmp_path))

    checkpoint = {
        "trace_id": "r-test1234",
        "user_goal": "Analyze the logs",
        "messages": [{"role": "user", "content": "Analyze the logs"}],
        "step": 3,
        "goal_idx": 0,
        "max_steps": 8,
        "json_fail_streak": 0,
        "model": "test-model",
        "created_at": "2026-08-21T14:30:00Z",
        "error_info": {
            "type": "timeout",
            "message": "⏱️ Request timed out",
            "retryable": True,
            "detail": "...",
        },
    }
    store.save("r-test1234", checkpoint)

    llm = MagicMock()
    llm._active_idx = 0
    llm.llm_cfg = {"model": "test-model"}
    llm._trace_id = "r-test1234"
    llm.set_trace_id = MagicMock()

    controller = AgentController(
        llm=llm,
        tool_index=MagicMock(),
        memory=MagicMock(),
        checkpoint_store=store,
    )

    with patch("agent_controller.react_loop") as mock_loop:
        mock_loop.return_value = "success"
        result = controller.run("Analyze the logs", resume_from="r-test1234")

        assert result == "success"
        call_kwargs = mock_loop.call_args
        initial_state = call_kwargs.kwargs.get("initial_state")
        assert initial_state is not None
        assert initial_state.step == 3
        assert initial_state.max_steps == 8
        assert initial_state.messages == checkpoint["messages"]

        # The ReactContext must be built with the checkpoint's trace_id, not a
        # fresh trace. This ensures successful completion deletes the original
        # checkpoint and resumed error checkpoints correlate to the same trace.
        ctx = call_kwargs.args[0]
        assert ctx.trace_id == "r-test1234"
        llm.set_trace_id.assert_called_with("r-test1234")


def test_run_without_resume_from_has_no_initial_state(tmp_path):
    """Verify that run() without resume_from passes no initial_state."""
    store = CheckpointStore(str(tmp_path))

    llm = MagicMock()
    llm._active_idx = 0
    llm.llm_cfg = {"model": "test-model"}
    llm.set_trace_id = MagicMock()

    controller = AgentController(
        llm=llm,
        tool_index=MagicMock(),
        memory=MagicMock(),
        checkpoint_store=store,
    )

    with patch("agent_controller.react_loop") as mock_loop:
        mock_loop.return_value = "success"
        controller.run("test goal")

        call_kwargs = mock_loop.call_args
        initial_state = call_kwargs.kwargs.get("initial_state")
        assert initial_state is None


def test_run_with_resume_from_nonexistent_checkpoint(tmp_path):
    """Verify that run(resume_from=...) with missing checkpoint proceeds normally."""
    store = CheckpointStore(str(tmp_path))

    llm = MagicMock()
    llm._active_idx = 0
    llm.llm_cfg = {"model": "test-model"}
    llm.set_trace_id = MagicMock()

    controller = AgentController(
        llm=llm,
        tool_index=MagicMock(),
        memory=MagicMock(),
        checkpoint_store=store,
    )

    with patch("agent_controller.react_loop") as mock_loop:
        mock_loop.return_value = "success"
        controller.run("test goal", resume_from="r-nonexistent")

        call_kwargs = mock_loop.call_args
        initial_state = call_kwargs.kwargs.get("initial_state")
        assert initial_state is None


def test_startup_scan_with_checkpoints_sends_notification(tmp_path):
    """Verify that startup scan sends notification when checkpoints exist."""
    store = CheckpointStore(str(tmp_path))
    checkpoint = {
        "trace_id": "r-test1234",
        "user_goal": "Analyze the logs",
        "messages": [],
        "step": 3,
        "goal_idx": 0,
        "max_steps": 8,
        "json_fail_streak": 0,
        "model": "test-model",
        "created_at": "2026-08-21T14:30:00Z",
        "error_info": {
            "type": "timeout",
            "message": "⏱️ Request timed out",
            "retryable": True,
            "detail": "...",
        },
    }
    store.save("r-test1234", checkpoint)

    notifications = []

    def notify(msg):
        notifications.append(msg)

    # Simulate the startup scan logic from main.py.
    checkpoints = store.list()
    if checkpoints:
        most_recent = checkpoints[0]
        goal = most_recent.get("user_goal", "?")[:60]
        if len(checkpoints) == 1:
            notify(f"💾 Found unfinished run: '{goal}'. Send /resume to continue.")
        else:
            notify(
                f"💾 Found {len(checkpoints)} unfinished runs. Most recent: '{goal}'. "
                "Send /resume to see all."
            )

    assert len(notifications) == 1
    assert "Analyze the logs" in notifications[0]
    assert "/resume" in notifications[0]


def test_startup_scan_no_checkpoints_sends_no_notification(tmp_path):
    """Verify that startup scan sends no notification when no checkpoints exist."""
    store = CheckpointStore(str(tmp_path))

    notifications = []

    def notify(msg):
        notifications.append(msg)

    checkpoints = store.list()
    if checkpoints:
        most_recent = checkpoints[0]
        goal = most_recent.get("user_goal", "?")[:60]
        notify(f"💾 Found unfinished run: '{goal}'. Send /resume to continue.")

    assert len(notifications) == 0

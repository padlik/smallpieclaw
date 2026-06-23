"""P1: Scheduler loop-survival guard tests.

Verifies that an exception inside _run_loop does not kill the scheduler thread:
the loop continues iterating after an error, and the wait always fires.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch



def _make_scheduler() -> object:
    """Return a minimal Scheduler whose _stop_event stops after two waits."""
    from scheduler import Scheduler

    cfg = {
        "telegram": {"bot_token": "x", "allowed_user_ids": []},
        "agent":    {"default_model": "test"},
    }
    s = Scheduler(cfg, notify_fn=lambda _: None)
    return s


class TestSchedulerLoopSurvival:
    """_run_loop must survive an unhandled exception in any iteration."""

    def test_exception_in_process_pending_does_not_kill_loop(self):
        """If _process_pending_commands() raises, the loop runs another iteration."""
        s = _make_scheduler()

        call_count = [0]
        stop_after = 2

        class _FakeEvent:
            def is_set(self):
                return call_count[0] >= stop_after

            def wait(self, timeout=30):
                call_count[0] += 1

        s._stop_event = _FakeEvent()

        iteration_results: list[str] = []

        def bad_process_pending():
            if call_count[0] == 0:
                raise RuntimeError("simulated failure in iteration 1")
            iteration_results.append("ok")

        with patch.object(s, "_process_pending_commands", side_effect=bad_process_pending):
            with patch.object(s, "_check_long_running_agents"):
                with patch("scheduler.schedule") as mock_schedule:
                    mock_schedule.run_pending = MagicMock()
                    s._warn_minutes = 0  # suppress long-agent check
                    s._jobs_meta = {}    # no cron jobs to fire
                    s._run_loop()

        # Loop ran at least a second iteration despite the first failing
        assert len(iteration_results) >= 1

    def test_exception_logged_not_swallowed(self):
        """Logger.exception must be called when an exception occurs."""
        s = _make_scheduler()

        call_count = [0]

        class _FakeEvent:
            def is_set(self):
                return call_count[0] >= 2

            def wait(self, timeout=30):
                call_count[0] += 1

        s._stop_event = _FakeEvent()
        s._jobs_meta = {}
        s._warn_minutes = 0

        with patch.object(s, "_process_pending_commands", side_effect=ValueError("boom")):
            with patch("scheduler.schedule") as mock_schedule:
                mock_schedule.run_pending = MagicMock()
                with patch("scheduler.logger") as mock_logger:
                    s._run_loop()

        mock_logger.exception.assert_called()

    def test_wait_still_runs_after_exception(self):
        """_stop_event.wait() must be called even when the loop body raises."""
        s = _make_scheduler()

        wait_calls = [0]

        class _FakeEvent:
            def is_set(self):
                return wait_calls[0] >= 2

            def wait(self, timeout=30):
                wait_calls[0] += 1

        s._stop_event = _FakeEvent()
        s._jobs_meta = {}
        s._warn_minutes = 0

        with patch.object(s, "_process_pending_commands", side_effect=RuntimeError("oops")):
            with patch("scheduler.schedule") as mock_schedule:
                mock_schedule.run_pending = MagicMock()
                s._run_loop()

        # wait() called at least twice (once per iteration), verifying it's in finally-equivalent position
        assert wait_calls[0] >= 2

    def test_exception_in_schedule_run_pending_survives(self):
        """An exception from schedule.run_pending() is also caught."""
        s = _make_scheduler()

        call_count = [0]

        class _FakeEvent:
            def is_set(self):
                return call_count[0] >= 2

            def wait(self, timeout=30):
                call_count[0] += 1

        s._stop_event = _FakeEvent()
        s._jobs_meta = {}
        s._warn_minutes = 0

        with patch.object(s, "_process_pending_commands"):
            with patch("scheduler.schedule") as mock_schedule:
                mock_schedule.run_pending.side_effect = RuntimeError("schedule boom")
                s._run_loop()

        # If we reach here the loop did not propagate the exception
        assert call_count[0] >= 2

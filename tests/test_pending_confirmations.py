"""Tests for PendingConfirmations and its lifecycle integration."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

from builtin_executor import PendingConfirmations
from confirmation import ConfirmationManager
from sub_agent_supervisor import SubAgentSupervisor, SupervisionOptions


@pytest.fixture
def pending() -> PendingConfirmations:
    return PendingConfirmations()


class TestPendingConfirmationsBasics:
    def test_stage_and_take(self, pending: PendingConfirmations) -> None:
        pending.stage("t1", "file_write", {"path": "/tmp/x"})
        assert pending.take("t1") == ("file_write", {"path": "/tmp/x"})

    def test_take_is_atomic_and_removes_all_metadata(self, pending: PendingConfirmations) -> None:
        pending.stage(
            "t1", "file_write", {"path": "/tmp/x"},
            zone_path="/tmp/x", scope_owner="sa-1",
        )
        assert pending.take("t1") == ("file_write", {"path": "/tmp/x"})
        assert pending.zone_path("t1") == ""
        assert pending.scope("t1") is None
        assert pending.take("t1") is None

    def test_take_on_missing_returns_none(self, pending: PendingConfirmations) -> None:
        assert pending.take("missing") is None

    def test_discard_returns_true_when_present(self, pending: PendingConfirmations) -> None:
        pending.stage("t1", "file_write", {})
        assert pending.discard("t1") is True
        assert pending.discard("t1") is False

    def test_reset_clears_everything(self, pending: PendingConfirmations) -> None:
        pending.stage(
            "t1", "file_write", {}, zone_path="/tmp/x", scope_owner="sa-1",
        )
        pending.stage("t2", "file_read", {})
        pending.reset()
        assert pending.take("t1") is None
        assert pending.take("t2") is None
        assert pending.zone_path("t1") == ""
        assert pending.scope("t1") is None

    def test_scope_recorded_per_token(self, pending: PendingConfirmations) -> None:
        pending.stage("main", "file_write", {}, scope_owner=None)
        pending.stage("sub", "file_read", {}, scope_owner="sa-abc")
        assert pending.scope("main") is None
        assert pending.scope("sub") == "sa-abc"


class TestPendingConfirmationsConcurrency:
    def test_only_one_take_wins(self, pending: PendingConfirmations) -> None:
        pending.stage("token", "file_write", {"path": "/tmp/x"})
        results: list = []

        def take_it() -> None:
            results.append(pending.take("token"))

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.submit(take_it) for _ in range(8))

        wins = [r for r in results if r is not None]
        assert len(wins) == 1
        assert wins[0] == ("file_write", {"path": "/tmp/x"})


class TestHeadlessConfirmationCancellation:
    def test_cancel_event_denies_and_invalidates_pending_decision(self) -> None:
        manager = ConfirmationManager()
        cancel_event = threading.Event()
        prompted = threading.Event()
        token = "cancel-race"
        decisions = []
        errors = []

        def request_confirmation() -> None:
            try:
                decisions.append(
                    manager.request_headless_confirmation(
                        token,
                        "file_write",
                        "write a file",
                        prompt_fn=lambda *_args: prompted.set(),
                        caller_tag="sa-cancel-race",
                        cancel_event=cancel_event,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - captured for red-phase evidence
                errors.append(exc)

        request_thread = threading.Thread(
            target=request_confirmation
        )
        request_thread.start()
        assert prompted.wait(timeout=1.0)

        cancel_event.set()
        request_thread.join(timeout=0.5)

        assert request_thread.is_alive() is False
        assert errors == []
        assert decisions == [False]
        assert manager.signal_headless_confirmation(token, True) is False


class TestExecutorCompatShims:
    def test_zone_paths_compat_view(self, make_builtin_executor) -> None:
        executor = make_builtin_executor()
        executor._pending_confirmations.stage("t1", "file_write", {}, zone_path="/tmp/x")
        # Legacy callback-style access still works through the property shim.
        assert executor._zone_paths.get("t1") == "/tmp/x"

    def test_confirm_and_cancel_use_locked_take(self, make_builtin_executor, tmp_path) -> None:
        executor = make_builtin_executor()
        out_file = str(tmp_path / "out.txt")
        executor._pending_confirmations.stage(
            "t1", "file_write", {"path": out_file, "content": "hi", "mode": "w"},
        )
        result = executor.confirm("t1")
        assert result["success"] is True
        assert open(out_file).read() == "hi"
        # Second confirm is a no-op (token already taken).
        assert executor.confirm("t1").get("success") is False

    def test_cancel_after_take_is_no_op(self, make_builtin_executor, tmp_path) -> None:
        executor = make_builtin_executor()
        executor._pending_confirmations.stage(
            "t1", "file_write", {"path": str(tmp_path / "x"), "content": "hi", "mode": "w"},
        )
        executor.cancel("t1")
        assert executor._pending_confirmations.take("t1") is None


class TestAgentControllerRunBoundaries:
    def test_depth0_run_entry_clears_main_prompt_grants(
        self, make_agent_controller, make_builtin_executor
    ) -> None:
        from confirmation import GrantLifetime

        executor = make_builtin_executor()
        ctrl = make_agent_controller(builtin_executor=executor)
        ctrl._confirmation.add_grant("file_write", "/data", GrantLifetime.PROMPT)
        assert ctrl._confirmation.check_grant("file_write", "/data/x.txt") is True

        with patch("agent_controller.react_loop", return_value="done"):
            with patch(
                "agent_controller.AgentRuntime.build_react_context", return_value=MagicMock()
            ):
                with patch("agent_controller.bind_run_context"):
                    result = ctrl.run("hello")

        assert result == "done"
        assert ctrl._confirmation.check_grant("file_write", "/data/x.txt") is False

    def test_reset_task_calls_clear_all(self, make_agent_controller) -> None:
        from confirmation import GrantLifetime

        ctrl = make_agent_controller()
        ctrl._confirmation.add_grant("file_read", "/data", GrantLifetime.SESSION)
        assert ctrl._confirmation.check_grant("file_read", "/data/x.txt") is True

        # reset_task saves nothing when working memory is empty; clear_all must still run.
        ctrl.reset_task(save=False)
        assert ctrl._confirmation.check_grant("file_read", "/data/x.txt") is False


class TestRunScopedPendingReset:
    def test_depth0_run_finally_resets_pending_confirmations(
        self, make_agent_controller, make_builtin_executor
    ) -> None:
        executor = make_builtin_executor()
        ctrl = make_agent_controller(builtin_executor=executor)
        executor._pending_confirmations.stage("t1", "file_write", {"path": "/tmp/x"})

        with patch("agent_controller.react_loop", return_value="done"):
            with patch(
                "agent_controller.AgentRuntime.build_react_context", return_value=MagicMock()
            ):
                with patch("agent_controller.bind_run_context"):
                    ctrl.run("hello")

        assert executor._pending_confirmations.take("t1") is None

    def test_pending_reset_waits_for_owned_child_drain(
        self, make_agent_controller, make_builtin_executor
    ) -> None:
        drain_entered = threading.Event()
        release_drain = threading.Event()

        class BlockingSupervisor:
            def drain_owned_children(self, owner_turn_id: str, *, cancel: bool) -> None:
                assert owner_turn_id.startswith("r-")
                assert cancel is True
                drain_entered.set()
                release_drain.wait(timeout=5.0)

        executor = make_builtin_executor()
        executor._supervisor = BlockingSupervisor()
        ctrl = make_agent_controller(builtin_executor=executor)
        executor._pending_confirmations.stage(
            "pending-token",
            "file_write",
            {"path": "/tmp/x"},
        )
        confirmation_event = threading.Event()
        ctrl._confirmation._confirm_events["confirm-token"] = confirmation_event
        results = []

        try:
            with patch("agent_controller.react_loop", return_value="done"), \
                 patch("agent_controller.AgentRuntime.build_react_context", return_value=MagicMock()), \
                 patch("agent_controller.bind_run_context"):
                run_thread = threading.Thread(
                    target=lambda: results.append(ctrl.run("hello"))
                )
                run_thread.start()
                assert drain_entered.wait(timeout=1.0)

                assert executor._pending_confirmations.scope("pending-token") is None
                assert executor._pending_confirmations.take("pending-token") == (
                    "file_write",
                    {"path": "/tmp/x"},
                )
                executor._pending_confirmations.stage(
                    "pending-token",
                    "file_write",
                    {"path": "/tmp/x"},
                )
                ctrl.resume("confirm-token", True)
                assert confirmation_event.wait(timeout=0.5)

                release_drain.set()
                run_thread.join(timeout=5.0)
        finally:
            release_drain.set()

        assert results == ["done"]
        assert executor._pending_confirmations.take("pending-token") is None


class TestStopDrainKeepsConfirmationCallbacksLive:
    def test_stop_during_drain_still_resolves_confirmation_callbacks(
        self, make_agent_controller, make_builtin_executor
    ) -> None:
        drain_entered = threading.Event()
        release_drain = threading.Event()

        class BlockingSupervisor:
            def drain_owned_children(self, owner_turn_id: str, *, cancel: bool) -> None:
                drain_entered.set()
                release_drain.wait(timeout=5.0)

        executor = make_builtin_executor()
        executor._supervisor = BlockingSupervisor()
        ctrl = make_agent_controller(builtin_executor=executor)
        confirmation_event = threading.Event()
        ctrl._confirmation._confirm_events["child-token"] = confirmation_event
        results = []

        try:
            with patch("agent_controller.react_loop", return_value="done"), \
                 patch("agent_controller.AgentRuntime.build_react_context", return_value=MagicMock()), \
                 patch("agent_controller.bind_run_context"):
                run_thread = threading.Thread(
                    target=lambda: results.append(ctrl.run("hello"))
                )
                run_thread.start()
                assert drain_entered.wait(timeout=1.0)

                ctrl.cancel()
                assert executor._coordinator is ctrl._confirmation
                ctrl.resume("child-token", True)
                assert confirmation_event.wait(timeout=0.5)

                release_drain.set()
                run_thread.join(timeout=5.0)
        finally:
            release_drain.set()

        assert results == ["done"]
        assert executor._coordinator is None


class TestSubAgentGrantScopeCallback:
    def test_supervision_options_accepts_grant_scope_cb(self) -> None:
        called_with: list[str] = []

        def cb(agent_id: str) -> None:
            called_with.append(agent_id)

        options = SupervisionOptions(grant_scope_cb=cb)
        assert options.grant_scope_cb is cb

    def test_run_and_notify_finally_invokes_grant_scope_cb(self) -> None:
        from types import SimpleNamespace

        called_with: list[str] = []

        def cb(agent_id: str) -> None:
            called_with.append(agent_id)

        class FakeRunner:
            agent_id = "sa-callback"
            notify_fn = staticmethod(lambda _msg: None)
            short_term = MagicMock()
            _model_id = "test"
            model_id = "test"
            _cancel_event = MagicMock()
            _llm = MagicMock()
            _agent = MagicMock()

            def run(self, task):
                return "done"

            def close(self):
                pass

            @property
            def trace_id(self):
                return None

        class FakeRecord:
            started_at = 0
            status = "running"
            result = ""
            timeout_cancelled = False
            iteration = 0
            max_iterations = 10

            def signal_result(self):
                pass

            def cancel(self):
                pass

        runner = FakeRunner()
        record = FakeRecord()
        supervisor = SubAgentSupervisor(max_subagents=2)
        request = SimpleNamespace(
            task="task",
            label="label",
            context_key=None,
            data_dir="/tmp",
            save_context=None,
            notify_html_fn=None,
            response_format="text",
        )
        options = SupervisionOptions(
            job_tag="job", grant_scope_cb=cb, result_log_cb=None, notify=False
        )

        supervisor._run_and_notify(request, options, runner, record)

        assert called_with == ["sa-callback"]

    def test_grant_scope_cb_exception_does_not_break_completion(self) -> None:
        from types import SimpleNamespace

        finish_calls: list[str] = []

        class FakeRunner:
            agent_id = "sa-cb-err"
            notify_fn = staticmethod(lambda _msg: None)
            short_term = MagicMock()
            _model_id = "test"
            model_id = "test"
            _cancel_event = MagicMock()
            _llm = MagicMock()
            _agent = MagicMock()

            def run(self, task):
                return "done"

            def close(self):
                pass

            @property
            def trace_id(self):
                return None

        class FakeRecord:
            started_at = 0
            status = "running"
            result = ""
            timeout_cancelled = False
            iteration = 0
            max_iterations = 10

            def signal_result(self):
                pass

            def cancel(self):
                pass

        runner = FakeRunner()
        record = FakeRecord()
        supervisor = SubAgentSupervisor(max_subagents=2)
        request = SimpleNamespace(
            task="task",
            label="label",
            context_key=None,
            data_dir="/tmp",
            save_context=None,
            notify_html_fn=None,
            response_format="text",
        )

        def bad_cb(agent_id: str) -> None:
            raise RuntimeError("boom")

        options = SupervisionOptions(
            job_tag="job", grant_scope_cb=bad_cb, finish_cb=finish_calls.append, notify=False
        )

        supervisor._run_and_notify(request, options, runner, record)

        assert finish_calls == ["job"]

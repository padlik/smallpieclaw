"""Tests for the SubAgentSupervisor boundary and per-submission options.

Covers OpenSpec change ``extract-sub-agent-supervisor`` tasks 1.1-1.6:

- scheduler synchronous rejection cleanup (last_error, notify, _running_jobs)
- per-submission scheduler callback isolation for concurrent launches
- stale-notification suppression through the actual supervisor lifecycle
- context/supervision channel separation (controls never enter context_payload)
- graph-memory non-admission of sub-agent results
- capacity + SubAgentRecord.source preservation for spawned/scheduled launches
- supervisor-owned pool shutdown cancels active runs
"""

from __future__ import annotations

import contextlib
import threading
from contextlib import ExitStack
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from outcome_utils import read_execution, with_execution

from confirmation import GrantLifetime
from memory_store import ShortTermMemory
from sub_agent_registry import SubAgentRecord, SubAgentRegistry, get_registry
from sub_agent_supervisor import (
    ORIGIN_ON_DEMAND,
    ORIGIN_SCHEDULED,
    ExecutionOwnership,
    SubAgentSupervisor,
    SubmissionRequest,
    SupervisionOptions,
)
from tests.inline_futures import run_inline_future


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------

class FakeRunner:
    """Minimal SubAgentRunner stand-in with observable lifecycle hooks."""

    def __init__(self, agent_id: str = "sa-x", result: str = "done",
                 block: bool = False, model_id: str = "test-model"):
        self.agent_id = agent_id
        self._model_id = model_id
        self.model_id = model_id
        self._cancel_event = threading.Event()
        self._llm = MagicMock()  # cancel() calls close_http()
        self._agent = SimpleNamespace(max_iterations=8, _on_step=None, _trace_id=None)
        self._short_term = ShortTermMemory(max_turns=50)
        self.short_term = self._short_term
        self.trace_id = None
        self.notify_calls: list[str] = []
        self.closed = False
        self.saw_cancel = False
        self._result = result
        self._block = block
        self._done_event = threading.Event()

    def run(self, task):
        if self._block:
            self._cancel_event.wait(timeout=5.0)
        if self._cancel_event.is_set():
            self.saw_cancel = True
            return "[Cancelled]"
        return self._result

    def notify_fn(self, msg):
        self.notify_calls.append(msg)

    def close(self):
        self.closed = True
        self._done_event.set()


def _seq_factory(runners):
    """Return a factory that yields the given runners in order."""
    it = iter(runners)

    def factory(**_kwargs):
        return next(it)

    return factory


def _wait_event(event: threading.Event, timeout: float = 5.0) -> bool:
    return event.wait(timeout=timeout)


@pytest.fixture(autouse=True)
def _clean_registry():
    """Isolate tests from the module-level singleton registry."""
    yield
    reg = get_registry()
    for rec in reg.list_active():
        rec.cancel()
    for rec in reg.list_active():
        reg.unregister(rec.agent_id)


# ---------------------------------------------------------------------------
# 1.4 — scheduler synchronous rejection
# ---------------------------------------------------------------------------

class TestSchedulerSynchronousRejection:
    """A synchronous rejection from the spawn path must clean up scheduler state."""

    def _sched(self, tmp_path, monkeypatch):
        from scheduler import Scheduler
        from xdg import xdg_paths

        config_path = tmp_path / "scheduler.toml"
        config_path.write_text("")
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        return Scheduler(
            config={"scheduler": {"enabled": False}, "agent": {"scheduled_max_iterations": 10}},
            notify_fn=MagicMock(),
            agent_fn=MagicMock(),
            scheduler_config_path=str(config_path),
            paths=xdg_paths("test-agent"),
        )

    def test_cap_rejection_sets_last_error_notifies_and_clears_running(self, tmp_path, monkeypatch):
        s = self._sched(tmp_path, monkeypatch)
        s._jobs_meta["capped_job"] = {"task": "do work", "enabled": True, "notify": True}

        mock_executor = MagicMock()
        mock_executor.spawn_agent = MagicMock(return_value=with_execution({
            "success": False,
            "output": "",
            "error": "spawn_agent: max_subagents cap reached (6/6).",
            "exit_code": -1,
        }, "refused"))
        s.builtin_executor = mock_executor
        s.notify = MagicMock()

        s._run_job("capped_job")

        # last_error recorded from the synchronous rejection
        assert "cap reached" in s._run_history["capped_job"]["last_error"]
        # operator notified on failure
        s.notify.assert_called_once()
        # running-jobs set is cleaned up so the job can fire again
        assert "capped_job" not in s._running_jobs


# ---------------------------------------------------------------------------
# per-submission callback isolation (concurrent scheduled launches)
# ---------------------------------------------------------------------------

class TestCallbackIsolation:
    """Concurrent scheduled launches must not overwrite each other's callbacks."""

    def test_concurrent_launches_keep_per_submission_callbacks(self, make_builtin_executor, tmp_path):
        runner_a = FakeRunner(agent_id="sa-a", result="A-done")
        runner_b = FakeRunner(agent_id="sa-b", result="B-done")
        exc = make_builtin_executor(
            sub_agent_factory=_seq_factory([runner_a, runner_b]),
            data_dir=str(tmp_path),
        )

        finish_calls: list[str] = []
        log_calls: list[tuple] = []
        done_a = threading.Event()
        done_b = threading.Event()

        def finish_cb(tag):
            finish_calls.append(tag)
            (done_a if tag == "job-a" else done_b).set()

        def result_log(**kwargs):
            log_calls.append((kwargs["tag"], kwargs["result"], kwargs["success"]))

        res_a = exc._exec_spawn_agent(
            {"task": "task A", "context_payload": {"note": "a"}},
            caller_depth=0,
            options=SupervisionOptions(
                job_tag="job-a", finish_cb=finish_cb, result_log_cb=result_log,
                notify=False,
            ),
        )
        res_b = exc._exec_spawn_agent(
            {"task": "task B", "context_payload": {"note": "b"}},
            caller_depth=0,
            options=SupervisionOptions(
                job_tag="job-b", finish_cb=finish_cb, result_log_cb=result_log,
                notify=False,
            ),
        )

        assert res_a["success"] and res_b["success"]
        assert _wait_event(done_a) and _wait_event(done_b)

        # Each finish callback fired exactly once, with its own tag.
        assert sorted(finish_calls) == ["job-a", "job-b"]
        # Each result-log callback recorded its own job's result — no crossover.
        recorded = {tag: result for (tag, result, _ok) in log_calls}
        assert recorded == {"job-a": "A-done", "job-b": "B-done"}


# ---------------------------------------------------------------------------
# 1.3 — stale-notification suppression via the real supervisor lifecycle
# ---------------------------------------------------------------------------

class TestStaleNotificationSuppression:
    def _spawn_blocking(self, make_builtin_executor, tmp_path, agent_id):
        runner = FakeRunner(agent_id=agent_id, block=True)
        exc = make_builtin_executor(
            sub_agent_factory=_seq_factory([runner]),
            data_dir=str(tmp_path),
        )
        res = exc._exec_spawn_agent(
            {"task": "long task", "context_payload": {"n": "x"}},
            caller_depth=0,
            trace_id="r-owner",
        )
        assert res["success"]
        record = get_registry().get(res["agent_id"])
        assert record is not None
        return exc, runner, record

    def test_timeout_cancel_suppresses_stale_notification(self, make_builtin_executor, tmp_path):
        exc, runner, record = self._spawn_blocking(make_builtin_executor, tmp_path, "sa-stale")

        # get_agent_result times out and auto-cancels the run.
        out = exc._exec_get_agent_result(
            {"agent_id": record.agent_id, "timeout": 0},
            caller_tag="main r-owner",
        )
        assert out["status"] == "timeout"
        assert record._timeout_cancelled is True

        assert _wait_event(record._result_event)
        assert record.status == "cancelled"
        # The timed-out run must NOT emit a stale cancellation notification.
        assert runner.notify_calls == []

    def test_manual_cancel_does_not_notify_independently(self, make_builtin_executor, tmp_path):
        exc, runner, record = self._spawn_blocking(make_builtin_executor, tmp_path, "sa-manual")

        record.cancel()  # e.g. operator /agents cancel — not a get_agent_result timeout
        assert record._timeout_cancelled is False

        assert _wait_event(record._result_event)
        assert record.status == "cancelled"
        assert runner.notify_calls == []


class TestOriginAwareTerminalDelivery:
    """Terminal delivery differs for turn-owned and scheduled executions."""

    @pytest.mark.parametrize("terminal", ["done", "failed", "cancelled", "timeout_cancelled"])
    def test_turn_owned_terminal_outcome_never_notifies(
        self, make_builtin_executor, tmp_path, terminal
    ):
        runner = FakeRunner(agent_id=f"sa-{terminal}", result="child result")
        if terminal == "failed":
            runner.run = MagicMock(side_effect=RuntimeError("child failed"))
        exc = make_builtin_executor(
            sub_agent_factory=_seq_factory([runner]),
            data_dir=str(tmp_path),
        )
        local_reg = SubAgentRegistry()

        def run_now(fn, *_args, **_kwargs):
            record = local_reg.get(runner.agent_id)
            assert record is not None
            if terminal in ("cancelled", "timeout_cancelled"):
                if terminal == "timeout_cancelled":
                    record._timeout_cancelled = True
                record.cancel()
            return run_inline_future(fn)

        with patch("sub_agent_registry.get_registry", return_value=local_reg), \
             patch.object(exc._supervisor._pool, "submit", side_effect=run_now):
            result = exc._exec_spawn_agent(
                {"task": "child work", "context_payload": {"parent": "turn"}},
                trace_id="r-owner",
            )

        assert result["success"] is True
        assert runner.notify_calls == []

    @pytest.mark.parametrize("terminal", ["done", "failed", "cancelled", "timeout_cancelled"])
    def test_scheduled_terminal_outcome_runs_callbacks(
        self, make_builtin_executor, tmp_path, terminal
    ):
        runner = FakeRunner(agent_id=f"sa-scheduled-{terminal}", result="job result")
        if terminal == "failed":
            runner.run = MagicMock(side_effect=RuntimeError("job failed"))
        exc = make_builtin_executor(
            sub_agent_factory=_seq_factory([runner]),
            data_dir=str(tmp_path),
        )
        local_reg = SubAgentRegistry()
        finish_calls = []
        result_calls = []

        def run_now(fn, *_args, **_kwargs):
            record = local_reg.get(runner.agent_id)
            assert record is not None
            if terminal in ("cancelled", "timeout_cancelled"):
                if terminal == "timeout_cancelled":
                    record._timeout_cancelled = True
                record.cancel()
            return run_inline_future(fn)

        with patch("sub_agent_registry.get_registry", return_value=local_reg), \
             patch.object(exc._supervisor._pool, "submit", side_effect=run_now):
            result = exc._exec_spawn_agent(
                {"task": "job work"},
                options=SupervisionOptions(
                    source="scheduled",
                    job_tag="nightly",
                    notify=False,
                    finish_cb=finish_calls.append,
                    result_log_cb=lambda **kwargs: result_calls.append(kwargs),
                ),
            )

        assert result["success"] is True
        assert finish_calls == ["nightly"]
        assert len(result_calls) == 1
        assert result_calls[0]["tag"] == "nightly"


class TestTurnOwnershipObligations:
    """The supervisor retains turn obligations through result consumption."""

    def test_completed_unretrieved_child_remains_an_obligation(
        self, make_builtin_executor, tmp_path
    ):
        runner = FakeRunner(agent_id="sa-unread", result="ready")
        exc = make_builtin_executor(
            sub_agent_factory=_seq_factory([runner]),
            data_dir=str(tmp_path),
        )

        with patch.object(
            exc._supervisor._pool,
            "submit",
            side_effect=run_inline_future,
        ):
            result = exc._exec_spawn_agent(
                {"task": "child work"},
                trace_id="r-owner",
            )

        assert result["success"] is True
        assert exc._supervisor.unaccounted_children("r-owner") == [
            ("sa-unread", "done")
        ]

        exc._supervisor.mark_result_retrieved("r-owner", "sa-unread")

        assert exc._supervisor.unaccounted_children("r-owner") == []

    def test_ownerless_direct_spawn_result_is_readable_without_turn_obligation(
        self, make_builtin_executor, tmp_path
    ):
        runner = FakeRunner(agent_id="sa-legacy", result="legacy ready")
        exc = make_builtin_executor(
            sub_agent_factory=_seq_factory([runner]),
            data_dir=str(tmp_path),
        )

        with patch.object(
            exc._supervisor._pool,
            "submit",
            side_effect=run_inline_future,
        ):
            spawned = exc._exec_spawn_agent({"task": "legacy work"})

        outcome = exc._exec_get_agent_result(
            {"agent_id": spawned["agent_id"], "timeout": 1}
        )

        assert outcome["success"] is True
        assert outcome["result"] == "legacy ready"
        assert exc._supervisor._owned_children == {}

    def test_wait_timeout_without_cancel_keeps_running_obligation(
        self, make_builtin_executor, tmp_path
    ):
        runner = FakeRunner(agent_id="sa-timeout-owned", block=True)
        exc = make_builtin_executor(
            sub_agent_factory=_seq_factory([runner]),
            data_dir=str(tmp_path),
        )
        result = exc._exec_spawn_agent(
            {"task": "long child work"},
            trace_id="r-owner",
        )

        outcome = exc._exec_get_agent_result(
            {
                "agent_id": result["agent_id"],
                "timeout": 0,
                "cancel_on_timeout": False,
            },
            caller_tag="main r-owner",
        )

        assert outcome["status"] == "timeout"
        assert exc._supervisor.unaccounted_children("r-owner") == [
            ("sa-timeout-owned", "running")
        ]
        runner._cancel_event.set()
        assert runner._done_event.wait(timeout=5.0)

    def test_resumed_turn_cannot_access_prior_turn_record(self):
        prior = SubAgentRecord(
            agent_id="sa-prior",
            label="on-demand",
            model="test-model",
            task_preview="prior work",
            started_at=0.0,
            source="on-demand",
        )
        prior.ownership = ExecutionOwnership(
            origin="on-demand",
            owner_turn_id="turn-before-resume",
        )

        assert SubAgentSupervisor.turn_can_access_result(
            prior, "turn-after-resume"
        ) is False

    def test_timeout_cancellation_accounts_while_cleanup_is_pending(
        self, make_builtin_executor, tmp_path
    ):
        class GatedRunner(FakeRunner):
            """Observes cancellation, then stays inside run() until released.

            This makes the advertised 'cleanup pending' state a real precondition
            instead of a race against the worker returning and finishing cleanup.
            """

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.entered = threading.Event()
                self.cancel_seen = threading.Event()
                self.release = threading.Event()

            def run(self, task):
                self.entered.set()
                self._cancel_event.wait(timeout=5.0)
                self.saw_cancel = self._cancel_event.is_set()
                self.cancel_seen.set()
                self.release.wait(timeout=5.0)  # after cancellation, before run() returns
                return "[Cancelled]"

        runner = GatedRunner(agent_id="sa-timeout-draining", block=True)
        try:
            exc = make_builtin_executor(
                sub_agent_factory=_seq_factory([runner]),
                data_dir=str(tmp_path),
            )
            result = exc._exec_spawn_agent(
                {"task": "long child work"},
                trace_id="r-owner",
            )
            assert runner.entered.wait(timeout=5.0)

            outcome = exc._exec_get_agent_result(
                {"agent_id": result["agent_id"], "timeout": 0},
                caller_tag="main r-owner",
            )

            assert outcome["status"] == "timeout"
            assert runner.cancel_seen.wait(timeout=5.0)
            # The worker is held inside run(): cleanup is genuinely pending here.
            assert exc._supervisor.unaccounted_children("r-owner") == []
            assert exc._supervisor.draining_children("r-owner") == [
                (runner.agent_id, "running")
            ]
        finally:
            runner.release.set()
        assert runner._done_event.wait(timeout=5.0)

    def test_operator_cancellation_accounts_child_before_cleanup(
        self, make_builtin_executor, tmp_path
    ):
        runner = FakeRunner(agent_id="sa-operator-cancel", block=True)
        exc = make_builtin_executor(
            sub_agent_factory=_seq_factory([runner]),
            data_dir=str(tmp_path),
        )
        result = exc._exec_spawn_agent(
            {"task": "long child work"},
            trace_id="r-owner",
        )
        record = get_registry().get(result["agent_id"])
        assert record is not None

        record.cancel()

        assert exc._supervisor.unaccounted_children("r-owner") == []
        assert runner._done_event.wait(timeout=5.0)


class TestOwnedCleanupFailureIsolation:
    """Every cleanup stage is attempted before the owned child is released."""

    @pytest.mark.parametrize(
        "failure_point",
        ["deregister_run", "runner.close", "clear_run_context", "finish_cb"],
    )
    def test_cleanup_failure_is_reported_without_stranding_turn(
        self, make_builtin_executor, tmp_path, failure_point
    ):
        runner = FakeRunner(agent_id=f"sa-cleanup-{failure_point}")
        exc = make_builtin_executor(
            sub_agent_factory=_seq_factory([runner]),
            data_dir=str(tmp_path),
        )
        options = SupervisionOptions(
            finish_cb=(
                MagicMock(side_effect=RuntimeError(failure_point))
                if failure_point == "finish_cb"
                else None
            )
        )

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    exc._supervisor._pool,
                    "submit",
                    side_effect=run_inline_future,
                )
            )
            if failure_point == "deregister_run":
                stack.enter_context(
                    patch(
                        "sub_agent_registry.deregister_run",
                        side_effect=RuntimeError(failure_point),
                    )
                )
            elif failure_point == "runner.close":
                runner.close = MagicMock(side_effect=RuntimeError(failure_point))
            elif failure_point == "clear_run_context":
                stack.enter_context(
                    patch(
                        "sub_agent_supervisor.clear_run_context",
                        side_effect=RuntimeError(failure_point),
                    )
                )

            spawned = exc._exec_spawn_agent(
                {"task": "child work"},
                trace_id="r-owner",
                options=options,
            )

        assert spawned["success"] is True
        errors = exc._supervisor.drain_owned_children("r-owner", cancel=False)
        assert any(failure_point in error for error in errors)


class TestBoundedTurnDrain:
    """A non-cooperative child cannot hold the operator turn forever."""

    def _spawn_stuck(self, make_builtin_executor, tmp_path):
        release_close = threading.Event()
        runner = FakeRunner(agent_id="sa-stuck")
        runner.close = lambda: release_close.wait(timeout=5.0)
        exc = make_builtin_executor(
            sub_agent_factory=_seq_factory([runner]),
            data_dir=str(tmp_path),
        )
        spawned = exc._exec_spawn_agent({"task": "child work"}, trace_id="r-owner")
        assert spawned["success"] is True
        return exc, release_close

    def test_drain_expiry_reports_stuck_child_within_bound(
        self, make_builtin_executor, tmp_path
    ):
        import time

        exc, release_close = self._spawn_stuck(make_builtin_executor, tmp_path)
        try:
            started = time.monotonic()
            errors = exc._supervisor.drain_owned_children(
                "r-owner", cancel=True, timeout=0.2
            )
            elapsed = time.monotonic() - started
        finally:
            release_close.set()

        assert elapsed < 2.0
        assert any("sa-stuck" in error and "did not complete" in error for error in errors)

    def test_late_cleanup_of_expired_child_releases_its_record(
        self, make_builtin_executor, tmp_path
    ):
        exc, release_close = self._spawn_stuck(make_builtin_executor, tmp_path)
        try:
            exc._supervisor.drain_owned_children("r-owner", cancel=True, timeout=0.2)
            assert exc._supervisor.draining_children("r-owner") == [
                ("sa-stuck", "done")
            ]
        finally:
            release_close.set()

        deadline = threading.Event()
        for _ in range(50):
            if not exc._supervisor._owned_children:
                break
            deadline.wait(0.05)
        assert exc._supervisor._owned_children == {}


class TestExplicitPromptAssociation:
    """Prompt-child association follows immutable submission data."""

    def test_on_demand_child_associates_with_explicit_prompt_id(
        self, make_builtin_executor, tmp_path
    ):
        from prompt_registry import PromptRegistry

        prompt_registry = PromptRegistry(data_dir=str(tmp_path / "prompts"))
        explicit = prompt_registry.start("r-owner", "explicit prompt")
        ambient = prompt_registry.start("r-ambient", "ambient prompt")
        runner = FakeRunner(agent_id="sa-explicit-prompt")
        exc = make_builtin_executor(
            sub_agent_factory=_seq_factory([runner]),
            data_dir=str(tmp_path),
        )
        exc._prompt_registry = prompt_registry
        exc._current_prompt_id = ambient.prompt_id
        local_reg = SubAgentRegistry()

        with patch("sub_agent_registry.get_registry", return_value=local_reg), \
             patch.object(exc._supervisor._pool, "submit", return_value=MagicMock()):
            result = exc._exec_spawn_agent(
                {"task": "child work"},
                trace_id="r-owner",
                options=SupervisionOptions(prompt_id=explicit.prompt_id),
            )

        assert result["success"] is True
        assert prompt_registry.get(explicit.prompt_id).sub_agent_ids == [
            runner.agent_id
        ]
        assert prompt_registry.get(ambient.prompt_id).sub_agent_ids == []
        event_log = (tmp_path / "prompts" / "prompts.jsonl").read_text()
        assert f'"agent_id": "{runner.agent_id}"' in event_log

    def test_scheduled_child_does_not_associate_with_ambient_prompt(
        self, make_builtin_executor, tmp_path
    ):
        from prompt_registry import PromptRegistry

        prompt_registry = PromptRegistry(data_dir=str(tmp_path / "prompts"))
        ambient = prompt_registry.start("r-ambient", "ambient prompt")
        runner = FakeRunner(agent_id="sa-scheduled-prompt")
        exc = make_builtin_executor(
            sub_agent_factory=_seq_factory([runner]),
            data_dir=str(tmp_path),
        )
        exc._prompt_registry = prompt_registry
        exc._current_prompt_id = ambient.prompt_id
        local_reg = SubAgentRegistry()

        with patch("sub_agent_registry.get_registry", return_value=local_reg), \
             patch.object(exc._supervisor._pool, "submit", return_value=MagicMock()):
            result = exc._exec_spawn_agent(
                {"task": "scheduled work"},
                options=SupervisionOptions(
                    source="scheduled",
                    job_tag="nightly",
                    notify=False,
                ),
            )

        assert result["success"] is True
        assert prompt_registry.get(ambient.prompt_id).sub_agent_ids == []


class TestAdmissionRollback:
    """Admission-time failures leave no stranded registry, turn, or child state."""

    def _make_request(self, tmp_path, runner, prompt_registry):
        from builtin_executor import _save_context

        ownership = ExecutionOwnership(
            origin=ORIGIN_ON_DEMAND,
            owner_turn_id="r-owner",
            prompt_id=None
            if prompt_registry is None
            else prompt_registry.start("r-owner", "owner prompt").prompt_id,
        )
        return SubmissionRequest(
            task="rollback task",
            response_format="text",
            label="on-demand",
            context_key=None,
            factory=lambda **_k: runner,
            factory_kwargs={},
            data_dir=str(tmp_path),
            notify_html_fn=None,
            save_context=_save_context,
            ownership=ownership,
            prompt_registry=prompt_registry,
        )

    def test_prompt_association_failure_rolls_back_admission(self, tmp_path):
        """A prompt-registry write failure leaves no registered or owned child."""
        from prompt_registry import PromptRegistry

        prompt_registry = PromptRegistry(data_dir=str(tmp_path / "prompts"))
        runner = FakeRunner(agent_id="sa-rollback-prompt")
        supervisor = SubAgentSupervisor(max_subagents=2)
        local_reg = SubAgentRegistry()

        with patch("sub_agent_registry.get_registry", return_value=local_reg), \
             patch.object(
                 prompt_registry,
                 "associate_sub_agent",
                 side_effect=OSError("disk full"),
             ):
            with pytest.raises(OSError):
                supervisor.submit(
                    self._make_request(tmp_path, runner, prompt_registry),
                    SupervisionOptions(notify=False),
                )

        assert local_reg.get(runner.agent_id) is None
        assert supervisor.unaccounted_children("r-owner") == []
        assert supervisor.draining_children("r-owner") == []

    def test_pool_submit_failure_rolls_back_admission(self, tmp_path):
        """A worker-pool submit failure leaves no registered or owned child."""
        from prompt_registry import PromptRegistry

        prompt_registry = PromptRegistry(data_dir=str(tmp_path / "prompts"))
        runner = FakeRunner(agent_id="sa-rollback-submit")
        supervisor = SubAgentSupervisor(max_subagents=2)
        local_reg = SubAgentRegistry()

        request = self._make_request(tmp_path, runner, prompt_registry)
        owner_prompt_id = request.ownership.prompt_id

        with patch("sub_agent_registry.get_registry", return_value=local_reg), \
             patch.object(
                 supervisor._pool,
                 "submit",
                 side_effect=RuntimeError("pool closed"),
             ):
            with pytest.raises(RuntimeError):
                supervisor.submit(request, SupervisionOptions(notify=False))

        assert local_reg.get(runner.agent_id) is None
        assert supervisor.unaccounted_children("r-owner") == []
        assert supervisor.draining_children("r-owner") == []
        assert runner.closed is True
        # The successfully recorded prompt association is rolled back too.
        assert prompt_registry.get(owner_prompt_id).sub_agent_ids == []


class TestRejectedSubmissionOwnership:
    """A rejected submission closes its constructed runner and rolls back state."""

    @staticmethod
    def _runner(agent_id: str):
        runner = FakeRunner(agent_id=agent_id)
        runner.close = MagicMock(side_effect=runner.close)
        runner.run = MagicMock()
        return runner

    @staticmethod
    def _request(tmp_path, runner, *, scheduled=False, prompt_registry=None):
        if scheduled:
            ownership = ExecutionOwnership(origin=ORIGIN_SCHEDULED)
        else:
            ownership = ExecutionOwnership(
                origin=ORIGIN_ON_DEMAND,
                owner_turn_id="r-owner",
                prompt_id=prompt_registry.start("r-owner", "p").prompt_id,
            )
        return SubmissionRequest(
            task="t",
            response_format="text",
            label="job" if scheduled else "on-demand",
            context_key=None,
            factory=lambda **_k: runner,
            factory_kwargs={},
            data_dir=str(tmp_path),
            ownership=ownership,
            prompt_registry=prompt_registry,
        )

    @staticmethod
    def _prompts(tmp_path):
        from prompt_registry import PromptRegistry

        return PromptRegistry(data_dir=str(tmp_path / "prompts"))

    def _assert_rejected(self, runner, reg, finish_cb):
        assert runner.close.call_count == 1
        assert reg.get(runner.agent_id) is None
        runner.run.assert_not_called()
        finish_cb.assert_not_called()

    def test_on_demand_prompt_association_failure_closes_runner(self, tmp_path):
        prompts = self._prompts(tmp_path)
        runner = self._runner("sa-own-prompt")
        supervisor = SubAgentSupervisor(max_subagents=1)
        reg = SubAgentRegistry()
        finish_cb = MagicMock()
        with patch("sub_agent_registry.get_registry", return_value=reg), \
             patch.object(prompts, "associate_sub_agent", side_effect=OSError("primary")):
            with pytest.raises(OSError, match="primary"):
                supervisor.submit(
                    self._request(tmp_path, runner, prompt_registry=prompts),
                    SupervisionOptions(notify=False, finish_cb=finish_cb),
                )
        self._assert_rejected(runner, reg, finish_cb)
        assert supervisor.draining_children("r-owner") == []

    def test_scheduled_executor_failure_closes_runner(self, tmp_path):
        runner = self._runner("sa-own-sched")
        supervisor = SubAgentSupervisor(max_subagents=1)
        reg = SubAgentRegistry()
        finish_cb = MagicMock()
        with patch("sub_agent_registry.get_registry", return_value=reg), \
             patch.object(supervisor._pool, "submit", side_effect=RuntimeError("primary")):
            with pytest.raises(RuntimeError, match="primary"):
                supervisor.submit(
                    self._request(tmp_path, runner, scheduled=True),
                    SupervisionOptions(
                        notify=False, source="scheduled", finish_cb=finish_cb
                    ),
                )
        self._assert_rejected(runner, reg, finish_cb)

    def test_registration_insert_then_raise_is_rolled_back(self, tmp_path):
        import sub_agent_registry

        runner = self._runner("sa-own-insert")
        supervisor = SubAgentSupervisor(max_subagents=1)
        reg = SubAgentRegistry()
        finish_cb = MagicMock()

        real_register = sub_agent_registry.register_run

        def insert_then_raise(r, **kwargs):
            real_register(r, **kwargs)
            raise RuntimeError("primary")

        with patch("sub_agent_registry.get_registry", return_value=reg), \
             patch("sub_agent_registry.register_run", insert_then_raise):
            with pytest.raises(RuntimeError, match="primary"):
                supervisor.submit(
                    self._request(tmp_path, runner, scheduled=True),
                    SupervisionOptions(
                        notify=False, source="scheduled", finish_cb=finish_cb
                    ),
                )
        self._assert_rejected(runner, reg, finish_cb)

    def test_registration_failure_before_insert_closes_runner(self, tmp_path):
        runner = self._runner("sa-own-noinsert")
        supervisor = SubAgentSupervisor(max_subagents=1)
        reg = SubAgentRegistry()
        finish_cb = MagicMock()
        with patch("sub_agent_registry.get_registry", return_value=reg), \
             patch("sub_agent_registry.register_run", side_effect=RuntimeError("primary")):
            with pytest.raises(RuntimeError, match="primary"):
                supervisor.submit(
                    self._request(tmp_path, runner, scheduled=True),
                    SupervisionOptions(
                        notify=False, source="scheduled", finish_cb=finish_cb
                    ),
                )
        self._assert_rejected(runner, reg, finish_cb)

    @pytest.mark.parametrize(
        "failing", ["owned", "close", "deregister", "prompt_remove"]
    )
    def test_rollback_stage_failure_does_not_skip_others_or_mask_primary(
        self, tmp_path, failing
    ):
        prompts = self._prompts(tmp_path)
        runner = self._runner("sa-own-stage")
        supervisor = SubAgentSupervisor(max_subagents=1)
        reg = SubAgentRegistry()
        finish_cb = MagicMock()
        real_dereg = __import__("sub_agent_registry").deregister_run
        dereg = MagicMock(side_effect=real_dereg)
        remove = MagicMock(side_effect=prompts.remove_sub_agent)
        owned = MagicMock(side_effect=supervisor._discard_owned)
        if failing == "close":
            runner.close.side_effect = OSError("close boom")
        elif failing == "deregister":
            dereg.side_effect = OSError("dereg boom")
        elif failing == "owned":
            owned.side_effect = OSError("owned boom")
        else:
            remove.side_effect = OSError("remove boom")
        with patch("sub_agent_registry.get_registry", return_value=reg), \
             patch("sub_agent_registry.deregister_run", dereg), \
             patch.object(supervisor, "_discard_owned", owned), \
             patch.object(prompts, "remove_sub_agent", remove), \
             patch.object(supervisor._pool, "submit", side_effect=RuntimeError("primary")):
            with pytest.raises(RuntimeError, match="primary"):
                supervisor.submit(
                    self._request(tmp_path, runner, prompt_registry=prompts),
                    SupervisionOptions(notify=False, finish_cb=finish_cb),
                )
        assert owned.call_count == 1
        assert runner.close.call_count == 1
        assert dereg.call_count == 1
        assert remove.call_count == 1
        if failing != "owned":  # the injected fault is the removal itself
            assert supervisor.draining_children("r-owner") == []
        runner.run.assert_not_called()
        finish_cb.assert_not_called()

    @pytest.mark.parametrize(
        "failing", ["owned", "prompt_remove", "deregister", "close"]
    )
    def test_cleanup_cancellation_continues_stages_and_keeps_primary(
        self, tmp_path, failing
    ):
        prompts = self._prompts(tmp_path)
        runner = self._runner("sa-own-cleanup-cancel")
        supervisor = SubAgentSupervisor(max_subagents=1)
        reg = SubAgentRegistry()
        finish_cb = MagicMock()
        real_dereg = __import__("sub_agent_registry").deregister_run
        dereg = MagicMock(side_effect=real_dereg)
        remove = MagicMock(side_effect=prompts.remove_sub_agent)
        owned = MagicMock(side_effect=supervisor._discard_owned)
        boom = KeyboardInterrupt("cleanup cancel")
        {"owned": owned, "prompt_remove": remove, "deregister": dereg}.get(
            failing, runner.close
        ).side_effect = boom
        primary = RuntimeError("primary")
        with patch("sub_agent_registry.get_registry", return_value=reg), \
             patch("sub_agent_registry.deregister_run", dereg), \
             patch.object(supervisor, "_discard_owned", owned), \
             patch.object(prompts, "remove_sub_agent", remove), \
             patch.object(supervisor._pool, "submit", side_effect=primary):
            with pytest.raises(RuntimeError) as caught:
                supervisor.submit(
                    self._request(tmp_path, runner, prompt_registry=prompts),
                    SupervisionOptions(notify=False, finish_cb=finish_cb),
                )
        assert caught.value is primary
        assert owned.call_count == 1
        assert remove.call_count == 1
        assert dereg.call_count == 1
        assert runner.close.call_count == 1
        runner.run.assert_not_called()
        finish_cb.assert_not_called()

    def test_cancellation_during_submission_propagates_after_rollback(self, tmp_path):
        runner = self._runner("sa-own-cancel")
        supervisor = SubAgentSupervisor(max_subagents=1)
        reg = SubAgentRegistry()
        with patch("sub_agent_registry.get_registry", return_value=reg), \
             patch.object(supervisor._pool, "submit", side_effect=KeyboardInterrupt):
            with pytest.raises(KeyboardInterrupt):
                supervisor.submit(
                    self._request(tmp_path, runner, scheduled=True),
                    SupervisionOptions(notify=False, source="scheduled"),
                )
        assert runner.close.call_count == 1
        assert reg.get(runner.agent_id) is None


# ---------------------------------------------------------------------------
# 1.6 — context / supervision channel separation
# ---------------------------------------------------------------------------

class TestChannelSeparation:
    def test_supervision_controls_never_enter_context_payload(self, make_builtin_executor, tmp_path):
        captured = {}

        def factory(**kwargs):
            captured.update(kwargs)
            return FakeRunner(agent_id="sa-sep")

        exc = make_builtin_executor(sub_agent_factory=factory, data_dir=str(tmp_path))

        with patch.object(exc._supervisor._pool, "submit", return_value=MagicMock()):
            exc._exec_spawn_agent(
                {"task": "do work"},
                caller_depth=0,
                options=SupervisionOptions(
                    job_tag="secret-job",
                    finish_cb=lambda _t: None,
                    result_log_cb=lambda **_k: None,
                    notify=False,
                ),
            )

        payload = captured.get("context_payload")
        assert isinstance(payload, dict)
        # No supervision control key or callback leaked into the model-facing payload.
        for banned in ("_job_tag", "_finish_cb", "_result_log_cb", "_notify",
                       "expandable", "job_tag", "finish_cb"):
            assert banned not in payload
        flat = repr(payload)
        assert "secret-job" not in flat


# ---------------------------------------------------------------------------
# graph-memory non-admission
# ---------------------------------------------------------------------------

class TestGraphMemoryNonAdmission:
    def test_completed_result_not_admitted_to_graph(self, make_builtin_executor, tmp_path):
        runner = FakeRunner(agent_id="sa-graph", result="a fact worth remembering")
        exc = make_builtin_executor(
            sub_agent_factory=_seq_factory([runner]),
            data_dir=str(tmp_path),
        )
        exc._graph_memory = MagicMock()  # type: ignore[attr-defined]
        exc._graph_memory_writer = MagicMock()  # type: ignore[attr-defined]

        with patch.object(exc._supervisor._pool, "submit",
                          side_effect=run_inline_future):
            res = exc._exec_spawn_agent(
                {"task": "produce a fact", "context_payload": {"n": "x"}},
                caller_depth=0,
                options=SupervisionOptions(notify=False),
            )

        assert res["success"]
        runner_result_admitted = exc._graph_memory.add_episode.called
        assert runner_result_admitted is False
        # The supervisor lifecycle touches neither the store nor the writer.
        assert exc._graph_memory.mock_calls == []
        assert exc._graph_memory_writer.mock_calls == []

    def test_scheduled_completion_not_admitted_to_graph(self, make_builtin_executor, tmp_path):
        runner = FakeRunner(agent_id="sa-graph-sched", result="a fact worth remembering")
        exc = make_builtin_executor(
            sub_agent_factory=_seq_factory([runner]),
            data_dir=str(tmp_path),
        )
        exc._graph_memory = MagicMock()  # type: ignore[attr-defined]
        exc._graph_memory_writer = MagicMock()  # type: ignore[attr-defined]

        with patch.object(exc._supervisor._pool, "submit",
                          side_effect=run_inline_future):
            res = exc._exec_spawn_agent(
                {"task": "produce a fact"},
                caller_depth=0,
                options=SupervisionOptions(source="scheduled", job_tag="nightly", notify=False),
            )

        assert res["success"]
        assert exc._graph_memory.mock_calls == []
        assert exc._graph_memory_writer.mock_calls == []


# ---------------------------------------------------------------------------
# 1.2 — capacity + SubAgentRecord.source preservation
# ---------------------------------------------------------------------------

class TestCapacityAndSourcePreservation:
    def test_spawned_record_source_is_on_demand_and_counts(self, make_builtin_executor, tmp_path):
        runner = FakeRunner(agent_id="sa-src")
        exc = make_builtin_executor(
            sub_agent_factory=_seq_factory([runner]),
            data_dir=str(tmp_path),
        )
        local_reg = SubAgentRegistry()

        with patch("sub_agent_registry.get_registry", return_value=local_reg), \
             patch.object(exc._supervisor._pool, "submit", return_value=MagicMock()):
            res = exc._exec_spawn_agent({"task": "x"}, caller_depth=0)

        rec = local_reg.get(res["agent_id"])
        assert rec is not None
        assert rec.source == "on-demand"
        assert local_reg.count_managed() == 1

    def test_scheduled_record_has_immutable_origin_without_turn_owner(
        self, make_builtin_executor, tmp_path
    ):
        runner = FakeRunner(agent_id="sa-scheduled-owner")
        exc = make_builtin_executor(
            sub_agent_factory=_seq_factory([runner]),
            data_dir=str(tmp_path),
        )
        local_reg = SubAgentRegistry()

        with patch("sub_agent_registry.get_registry", return_value=local_reg), \
             patch.object(exc._supervisor._pool, "submit", return_value=MagicMock()):
            res = exc._exec_spawn_agent(
                {"task": "scheduled work", "context_payload": {"ignored": "later"}},
                options=SupervisionOptions(source="scheduled", job_tag="nightly"),
            )

        record = local_reg.get(res["agent_id"])
        assert record is not None
        assert record.ownership.origin == "scheduled"
        assert record.ownership.owner_turn_id is None
        with pytest.raises(FrozenInstanceError):
            record.ownership.origin = "on-demand"

    def test_scheduled_launch_record_source_is_scheduled_and_counts(self, make_builtin_executor, tmp_path):
        """Scheduled launches (source via options) record source=scheduled and count."""
        runner = FakeRunner(agent_id="sa-sched-src")
        exc = make_builtin_executor(
            sub_agent_factory=_seq_factory([runner]),
            data_dir=str(tmp_path),
        )
        local_reg = SubAgentRegistry()

        with patch("sub_agent_registry.get_registry", return_value=local_reg), \
             patch.object(exc._supervisor._pool, "submit", return_value=MagicMock()):
            res = exc._exec_spawn_agent(
                {"task": "x", "context_payload": {"n": "y"}},
                caller_depth=0,
                options=SupervisionOptions(
                    job_tag="nightly", source="scheduled", notify=False,
                ),
            )

        rec = local_reg.get(res["agent_id"])
        assert rec.source == "scheduled"
        assert rec.label == "nightly"
        # Scheduled runs still count against the global capacity guard.
        assert local_reg.count_managed() == 1

    def test_cap_reached_rejects_before_submission(self, make_builtin_executor, tmp_path):
        runner = FakeRunner(agent_id="sa-capped")
        factory = MagicMock(return_value=runner)
        exc = make_builtin_executor(sub_agent_factory=factory, data_dir=str(tmp_path),
                              max_subagents=2)
        full_reg = MagicMock()
        full_reg.count_managed.return_value = 2

        with patch("sub_agent_registry.get_registry", return_value=full_reg), \
             patch.object(exc._supervisor._pool, "submit") as pool_submit:
            res = exc._exec_spawn_agent({"task": "x"}, caller_depth=0)

        assert res["success"] is False
        assert "cap reached" in res["error"]
        # No runner constructed and nothing submitted when rejected pre-admission.
        factory.assert_not_called()
        pool_submit.assert_not_called()


# ---------------------------------------------------------------------------
# supervisor-owned pool shutdown
# ---------------------------------------------------------------------------

class TestPoolShutdown:
    def test_shutdown_cancels_active_and_closes_pool(self, make_builtin_executor, tmp_path):
        runner = FakeRunner(agent_id="sa-shutdown", block=True)
        exc = make_builtin_executor(
            sub_agent_factory=_seq_factory([runner]),
            data_dir=str(tmp_path),
        )
        res = exc._exec_spawn_agent(
            {"task": "long", "context_payload": {"n": "x"}},
            caller_depth=0,
        )
        record = get_registry().get(res["agent_id"])
        assert record is not None

        exc.shutdown(graceful_timeout=3.0)

        # Active run was signalled to cancel and wound down before pool close.
        assert record._cancel_event.is_set()
        assert _wait_event(record._result_event)
        assert runner.saw_cancel is True
        # The supervisor-owned pool is shut down and rejects new work.
        with pytest.raises(RuntimeError):
            exc._supervisor._pool.submit(lambda: None)

    def test_supervisor_shutdown_without_active_agents(self):
        supervisor = SubAgentSupervisor(max_subagents=2)
        supervisor.shutdown(graceful_timeout=0.1)
        with pytest.raises(RuntimeError):
            supervisor._pool.submit(lambda: None)


# ---------------------------------------------------------------------------
# supervisor.submit rejection shape (invalid model)
# ---------------------------------------------------------------------------

class TestSubmitRejectionShape:
    def test_factory_value_error_returns_rejection_without_finish_cb(self, make_builtin_executor, tmp_path):
        def factory(**_kwargs):
            raise ValueError("Model 'nope' not found")

        exc = make_builtin_executor(sub_agent_factory=factory, data_dir=str(tmp_path))
        finish_calls: list[str] = []

        res = exc._exec_spawn_agent(
            {"task": "x", "context_payload": {"n": "x"}},
            caller_depth=0,
            options=SupervisionOptions(job_tag="j", finish_cb=finish_calls.append),
        )

        assert res["success"] is False
        assert res["error_type"] == "wrong_model_for_task"
        assert "details are withheld" in res["error"] and "nope" not in res["error"]
        # finish_cb must NOT fire on synchronous admission failure.
        assert finish_calls == []


class TestStoppingRefusalAtSpawnBoundary:
    """Only the typed stopping error becomes the model-facing refusal."""

    @staticmethod
    def _spawn(exc, **kw):
        return exc._exec_spawn_agent(
            {"task": "x", "context_payload": {"n": "x"}}, caller_depth=0, **kw
        )

    def test_stopping_supervisor_returns_refusal_dict(self, make_builtin_executor, tmp_path):
        runner = FakeRunner(agent_id="sa-stop")
        exc = make_builtin_executor(
            sub_agent_factory=_seq_factory([runner]), data_dir=str(tmp_path)
        )
        exc._supervisor.shutdown(graceful_timeout=0.1)
        finish_calls: list[str] = []

        res = self._spawn(
            exc, options=SupervisionOptions(job_tag="j", finish_cb=finish_calls.append)
        )

        assert res["success"] is False
        assert res["output"] == ""
        assert res["exit_code"] == -1
        assert res["error_type"] == "impossible_with_current_tools"
        assert res["recoverable"] is False
        assert "shutting down" in res["error"]
        assert "restart" in res["suggestion"].lower()
        assert read_execution(res) == "refused"
        assert runner.closed is True
        assert finish_calls == []

    @pytest.mark.parametrize("error", [
        RuntimeError("sub-agent supervisor is shutting down"),
        KeyboardInterrupt(),
    ])
    def test_generic_runtime_error_and_cancellation_propagate_unchanged(
        self, make_builtin_executor, tmp_path, error
    ):
        runner = FakeRunner(agent_id="sa-generic")
        exc = make_builtin_executor(
            sub_agent_factory=_seq_factory([runner]), data_dir=str(tmp_path)
        )
        try:
            with patch.object(exc._supervisor._pool, "submit", side_effect=error):
                with pytest.raises(type(error)) as caught:
                    self._spawn(exc)

            assert caught.value is error
            assert runner.closed is True
        finally:
            exc._supervisor.shutdown(graceful_timeout=0.1)


def test_submission_request_direct_submit(tmp_path):
    """SubAgentSupervisor.submit admits a run and returns an agent_id."""
    from builtin_executor import _save_context

    runner = FakeRunner(agent_id="sa-direct", result="ok")
    supervisor = SubAgentSupervisor(max_subagents=2)
    done = threading.Event()

    request = SubmissionRequest(
        task="direct task",
        response_format="text",
        label="on-demand",
        context_key=None,
        factory=lambda **_k: runner,
        factory_kwargs={},
        data_dir=str(tmp_path),
        notify_html_fn=None,
        save_context=_save_context,
    )
    options = SupervisionOptions(notify=False, finish_cb=lambda _t: done.set())

    try:
        res = supervisor.submit(request, options)
        assert res["success"] is True
        assert res["agent_id"] == "sa-direct"
        assert res["response_format"] == "text"
        assert done.wait(timeout=5.0)
        assert runner.closed is True
    finally:
        supervisor.shutdown(graceful_timeout=1.0)


# ---------------------------------------------------------------------------
# grant-scope expiry wiring
# ---------------------------------------------------------------------------

class TestGrantScopeExpiry:
    """Sub-agent prompt grants expire when the run terminates."""

    def test_executor_wires_grant_scope_cb_when_coordinator_present(self, make_builtin_executor, tmp_path):
        from confirmation import ConfirmationManager

        exc = make_builtin_executor(data_dir=str(tmp_path))
        exc._coordinator = ConfirmationManager()
        options = SupervisionOptions()
        exc._wire_grant_scope_expiry(options)
        assert options.grant_scope_cb is not None
        assert callable(options.grant_scope_cb)

    def test_executor_leaves_cb_none_without_coordinator(self, make_builtin_executor, tmp_path):
        exc = make_builtin_executor(data_dir=str(tmp_path))
        exc._coordinator = None
        options = SupervisionOptions()
        exc._wire_grant_scope_expiry(options)
        assert options.grant_scope_cb is None

    def test_executor_preserves_caller_supplied_grant_scope_cb(self, make_builtin_executor, tmp_path):
        exc = make_builtin_executor(data_dir=str(tmp_path))
        supplied = MagicMock()
        options = SupervisionOptions(grant_scope_cb=supplied)
        exc._wire_grant_scope_expiry(options)
        assert options.grant_scope_cb is supplied

    def test_spawn_agent_public_facade_wires_expiry(self, make_builtin_executor, tmp_path):
        from confirmation import ConfirmationManager

        runner = FakeRunner(agent_id="sa-facade")
        exc = make_builtin_executor(
            sub_agent_factory=_seq_factory([runner]),
            data_dir=str(tmp_path),
        )
        exc._coordinator = ConfirmationManager()
        local_reg = SubAgentRegistry()
        ledger = exc._coordinator.grant_ledger
        ledger.add("file_write", str(tmp_path), GrantLifetime.PROMPT, scope_owner="sa-facade")

        with patch("sub_agent_registry.get_registry", return_value=local_reg), \
             patch.object(exc._supervisor._pool, "submit",
                          side_effect=run_inline_future):
            res = exc.spawn_agent({"task": "x"})

        assert res["success"] is True
        assert ledger.check("file_write", str(tmp_path / "f.txt"), scope_owner="sa-facade") is False

    def test_spawn_agent_tool_path_wires_expiry(self, make_builtin_executor, tmp_path):
        from confirmation import ConfirmationManager

        runner = FakeRunner(agent_id="sa-tool")
        exc = make_builtin_executor(
            sub_agent_factory=_seq_factory([runner]),
            data_dir=str(tmp_path),
        )
        exc._coordinator = ConfirmationManager()
        local_reg = SubAgentRegistry()
        ledger = exc._coordinator.grant_ledger
        ledger.add("file_write", str(tmp_path), GrantLifetime.PROMPT, scope_owner="sa-tool")

        with patch("sub_agent_registry.get_registry", return_value=local_reg), \
             patch.object(exc._supervisor._pool, "submit",
                          side_effect=run_inline_future):
            res = exc._exec_spawn_agent({"task": "x"}, caller_depth=0)

        assert res["success"] is True
        assert ledger.check("file_write", str(tmp_path / "f.txt"), scope_owner="sa-tool") is False

    def test_supervisor_invokes_grant_scope_cb_on_terminal(self, tmp_path):
        from builtin_executor import _save_context
        from confirmation import ConfirmationManager, GrantLifetime

        runner = FakeRunner(agent_id="sa-cb")
        supervisor = SubAgentSupervisor(max_subagents=2)
        cm = ConfirmationManager()
        ledger = cm.grant_ledger
        ledger.add("file_write", str(tmp_path), GrantLifetime.PROMPT, scope_owner="sa-cb")

        def expire_scope(agent_id: str) -> None:
            ledger.clear_prompt_scope(agent_id)

        request = SubmissionRequest(
            task="direct task",
            response_format="text",
            label="on-demand",
            context_key=None,
            factory=lambda **_k: runner,
            factory_kwargs={},
            data_dir=str(tmp_path),
            notify_html_fn=None,
            save_context=_save_context,
        )
        options = SupervisionOptions(notify=False, grant_scope_cb=expire_scope)

        try:
            res = supervisor.submit(request, options)
            assert res["success"] is True
            assert res["agent_id"] == "sa-cb"
            # Wait for the background task to finish; it deregisters on terminal,
            # so wait on the runner's own event and then verify the callback ran.
            assert _wait_event(runner._done_event)
            assert runner.closed is True
            assert ledger.check("file_write", str(tmp_path / "f.txt"), scope_owner="sa-cb") is False
        finally:
            supervisor.shutdown(graceful_timeout=1.0)


# ---------------------------------------------------------------------------
# late cancellation vs. normal runner return
# ---------------------------------------------------------------------------

class TestLateCancellationTerminalDecision:
    """A cancel observed at the post-run decision wins over a normal return.

    Cancellation after the terminal outcome is published does not rewrite it.
    """

    @pytest.mark.parametrize("origin", ["ordinary", "scheduled"])
    def test_cancel_before_normal_return_finalizes_cancelled(
        self, make_builtin_executor, tmp_path, origin
    ):
        from sub_agent_supervisor import CANCELLED_SENTINEL

        runner = FakeRunner(agent_id=f"sa-late-cancel-{origin}")
        exc = make_builtin_executor(
            sub_agent_factory=_seq_factory([runner]),
            data_dir=str(tmp_path),
        )
        local_reg = SubAgentRegistry()
        seen: dict = {}
        finish_calls = []
        result_calls = []
        completion_calls = []

        def cancel_then_finish(task):
            # Real cancel path (record.cancel), then a normal return that
            # deliberately ignores the shared cancel_event.
            record = local_reg.get(runner.agent_id)
            assert record is not None
            seen["record"] = record
            record.cancel()
            seen["flag_before_return"] = (
                record.is_cancelled and runner._cancel_event.is_set()
            )
            return "normal finish"

        runner.run = cancel_then_finish
        scheduled = origin == "scheduled"
        options = SupervisionOptions(
            source="scheduled" if scheduled else "on-demand",
            job_tag="nightly" if scheduled else None,
            notify=scheduled,
            finish_cb=finish_calls.append,
            result_log_cb=lambda **kwargs: result_calls.append(kwargs),
            completion_cb=completion_calls.append,
        )

        with patch("sub_agent_registry.get_registry", return_value=local_reg), \
             patch.object(exc._supervisor._pool, "submit",
                          side_effect=run_inline_future):
            spawned = exc._exec_spawn_agent(
                {"task": "late cancel work"},
                trace_id="" if scheduled else "r-owner",
                options=options,
            )

        assert spawned["success"] is True
        record = seen["record"]
        assert seen["flag_before_return"] is True
        assert record.status == "cancelled"
        assert record.result == CANCELLED_SENTINEL
        assert record._result_event.is_set()
        assert local_reg.get(runner.agent_id) is None
        assert local_reg.get_completed(runner.agent_id) is record
        assert runner.closed is True
        assert runner.notify_calls == []
        assert len(result_calls) == 1
        assert result_calls[0]["result"] == CANCELLED_SENTINEL
        assert result_calls[0]["success"] is False
        assert len(finish_calls) == 1
        if scheduled:
            assert result_calls[0]["tag"] == "nightly"
            assert finish_calls == ["nightly"]
            assert [c.status for c in completion_calls] == ["cancelled"]
            assert completion_calls[0].result == CANCELLED_SENTINEL
            assert completion_calls[0].agent_id == runner.agent_id
        else:
            assert completion_calls == []

    def test_cancel_after_publication_does_not_rewrite_done(
        self, make_builtin_executor, tmp_path
    ):
        runner = FakeRunner(agent_id="sa-post-publication", result="normal finish")
        exc = make_builtin_executor(
            sub_agent_factory=_seq_factory([runner]),
            data_dir=str(tmp_path),
        )
        local_reg = SubAgentRegistry()
        seen: dict = {}
        finish_calls = []
        result_calls = []
        completion_calls = []

        def cancel_after_publication(**kwargs):
            # _finalize_terminal invokes result_log_cb synchronously on the
            # pool thread only after record.signal_result(); the event check
            # below proves the outcome is already published when we cancel.
            record = local_reg.get(runner.agent_id)
            assert record is not None
            seen["record"] = record
            seen["published"] = record._result_event.is_set()
            seen["status_at_cancel"] = record.status
            record.cancel()
            seen["cancelled_flag"] = record.is_cancelled
            result_calls.append(kwargs)

        def finish_cb(tag):
            seen["status_at_finish"] = seen["record"].status
            finish_calls.append(tag)

        with patch("sub_agent_registry.get_registry", return_value=local_reg), \
             patch.object(exc._supervisor._pool, "submit",
                          side_effect=run_inline_future):
            spawned = exc._exec_spawn_agent(
                {"task": "post publication work"},
                options=SupervisionOptions(
                    source="scheduled",
                    job_tag="nightly",
                    notify=True,
                    finish_cb=finish_cb,
                    result_log_cb=cancel_after_publication,
                    completion_cb=completion_calls.append,
                ),
            )

        assert spawned["success"] is True
        record = seen["record"]
        assert seen["published"] is True
        assert seen["status_at_cancel"] == "done"
        assert seen["cancelled_flag"] is True
        assert seen["status_at_finish"] == "done"
        assert record.status == "done"
        assert record.result == "normal finish"
        assert local_reg.get_completed(runner.agent_id) is record
        assert runner.closed is True
        assert len(result_calls) == 1
        assert result_calls[0]["result"] == "normal finish"
        assert result_calls[0]["success"] is True
        assert finish_calls == ["nightly"]
        assert [c.status for c in completion_calls] == ["done"]


# ---------------------------------------------------------------------------
# Task 7.2 regression: non-Main admission errors keep the generic rejection
# ---------------------------------------------------------------------------

GENERIC_MODEL_SUGGESTION = "Choose a model that exists in the configured model list."


class TestSubmitRejectionCompatibility:
    """Classification regression only (factory fault injection, not root proof):
    a generic ``ValueError`` and a registry ``model_not_found`` refusal keep the
    exact generic configured-model rejection when Main-gate guidance is added."""

    @pytest.mark.parametrize(
        "kind,expected_error",
        [
            ("value_error", "spawn_agent: model admission failed; details are withheld (check the model selector and /models)"),
            ("model_not_found", "spawn_agent: model registry model not found"),
        ],
        ids=["generic-value-error", "registry-model-not-found"],
    )
    def test_non_main_gate_value_errors_keep_generic_rejection(
        self, make_builtin_executor, tmp_path, kind: str, expected_error: str,
    ) -> None:
        """Both errors keep the generic suggestion, response fields and no finish_cb."""
        from model_registry import ModelRegistryError
        from outcome_utils import read_execution

        calls: list[dict] = []

        def factory(**kwargs: object) -> None:
            """Record the call, then raise the injected admission error."""
            calls.append(kwargs)
            if kind == "value_error":
                raise ValueError("Model 'nope' not found")
            raise ModelRegistryError("model_not_found")

        exc = make_builtin_executor(sub_agent_factory=factory, data_dir=str(tmp_path))
        finish_calls: list[str] = []

        res = exc._exec_spawn_agent(
            {"task": "x", "context_payload": {"n": "x"}},
            caller_depth=0,
            options=SupervisionOptions(job_tag="j", finish_cb=finish_calls.append),
        )

        assert len(calls) == 1
        assert res["success"] is False and "agent_id" not in res
        assert res["error"] == expected_error
        assert res["exit_code"] == -1
        assert res["error_type"] == "wrong_model_for_task"
        assert res["recoverable"] is False
        assert res["suggestion"] == GENERIC_MODEL_SUGGESTION
        assert read_execution(res) == "refused"
        # finish_cb must NOT fire on synchronous admission failure.
        assert finish_calls == []

    @pytest.mark.parametrize("code", ["malformed_selector", "unknown_connection"])
    def test_selector_shape_errors_give_actionable_connection_guidance(
        self, make_builtin_executor, tmp_path, code: str,
    ) -> None:
        """Task 4.1: a bad selector shape/connection says how to write one, with no selector echo."""
        from model_registry import ModelRegistryError

        def factory(**_kwargs: object) -> None:
            raise ModelRegistryError(code)

        exc = make_builtin_executor(sub_agent_factory=factory, data_dir=str(tmp_path))

        res = exc._exec_spawn_agent({"task": "x", "model": " secret-conn/m"}, caller_depth=0)

        assert res["success"] is False and res["error_type"] == "wrong_model_for_task"
        assert "<connection>/<model>" in res["suggestion"] and "configured" in res["suggestion"]
        assert "secret-conn" not in res["error"] and res["recoverable"] is False


# ---------------------------------------------------------------------------
# Accepted queued cancellation and shutdown handoff (tasks 2.1-2.5)
# ---------------------------------------------------------------------------

class _HoldRunner(FakeRunner):
    """Runner whose ``run`` blocks on an Event that ignores cancellation."""

    def __init__(self, agent_id: str):
        super().__init__(agent_id=agent_id)
        self.started = threading.Event()
        self.hold = threading.Event()
        self.run_calls = 0

    def run(self, task):
        self.run_calls += 1
        self.started.set()
        self.hold.wait(timeout=10.0)
        return "late"


class _QueuedHarness:
    """Real-executor supervisor with one blocked worker and one queued task."""

    def __init__(self, tmp_path):
        self.supervisor = SubAgentSupervisor(max_subagents=1)
        self.events: list[str] = []
        self.finish_cb = MagicMock(side_effect=lambda tag: self.events.append("finish"))
        self.grant_cb = MagicMock(side_effect=lambda aid: self.events.append("grant"))
        self.completion_cb = MagicMock()
        self.result_log_cb = MagicMock()
        self.saved: list[tuple[str, bool]] = []
        self.tmp_path = tmp_path
        self.blocker = _HoldRunner("sa-block")
        self.queued = _HoldRunner("sa-queued")
        self.queued.close = MagicMock(
            side_effect=lambda: self.events.append("close")
        )

    def request(self, runner, *, scheduled=False, context_key=None, ownership=None):
        def save(key, memory, data_dir):
            rec = get_registry().get(runner.agent_id) or self.record
            self.saved.append((key, rec._result_event.is_set()))
            self.events.append("save")

        return SubmissionRequest(
            task="t", response_format="text", label="job",
            context_key=context_key, factory=lambda **_k: runner,
            factory_kwargs={}, data_dir=str(self.tmp_path),
            ownership=ownership or ExecutionOwnership(
                origin=ORIGIN_SCHEDULED if scheduled else ORIGIN_ON_DEMAND
            ),
            save_context=save,
        )

    def options(self, **kw):
        return SupervisionOptions(
            notify=kw.pop("notify", True), finish_cb=self.finish_cb,
            grant_scope_cb=self.grant_cb, completion_cb=self.completion_cb,
            result_log_cb=self.result_log_cb, **kw,
        )

    def start(self, **kw):
        self.supervisor.submit(self.request(self.blocker), self.options())
        assert self.blocker.started.wait(5.0)
        self.record = None
        self.supervisor.submit(
            self.request(self.queued, **kw), self.options()
        )
        self.record = get_registry().get(self.queued.agent_id)

    def release(self):
        self.blocker.hold.set()
        self.queued.hold.set()
        self.supervisor._pool.shutdown(wait=True)


class TestQueuedCancellationTerminalCleanup:
    """Shutdown finalizes accepted-but-never-started work exactly once."""

    def test_shutdown_cancels_queued_runner_with_terminal_cleanup(self, tmp_path):
        h = _QueuedHarness(tmp_path)
        try:
            h.start(scheduled=True, context_key="ctx")
            h.supervisor.shutdown(graceful_timeout=0.2)

            assert h.queued.run_calls == 0
            assert h.record.status == "cancelled"
            assert h.record.result == "[Cancelled]"
            assert h.record._result_event.is_set()
            assert h.saved == [("ctx", False)]
            assert h.queued.close.call_count == 1
            assert h.grant_cb.call_args_list[-1].args == ("sa-queued",)
            assert h.finish_cb.call_count == 1
            assert h.events == ["save", "close", "grant", "finish"]
            assert get_registry().get("sa-queued") is None
        finally:
            h.release()

    def test_repeated_shutdown_does_not_repeat_terminal_effects(self, tmp_path):
        h = _QueuedHarness(tmp_path)
        try:
            h.start(scheduled=True, context_key="ctx")
            h.supervisor.shutdown(graceful_timeout=0.2)
            h.supervisor.shutdown(graceful_timeout=0.2)

            assert h.queued.close.call_count == 1
            assert h.finish_cb.call_count == 1
            assert [c.args for c in h.grant_cb.call_args_list].count(("sa-queued",)) == 1
            assert h.completion_cb.call_count == 1
            assert h.saved == [("ctx", False)]
        finally:
            h.release()

    def test_capacity_released_and_blocked_worker_not_closed(self, tmp_path):
        h = _QueuedHarness(tmp_path)
        try:
            h.blocker.close = MagicMock()
            h.start(scheduled=True)
            h.supervisor.shutdown(graceful_timeout=0.2)

            assert get_registry().get("sa-queued") is None
            assert h.blocker.close.call_count == 0  # started client stays worker-owned
        finally:
            h.release()

    def test_scheduled_notification_and_result_log_routed_once(self, tmp_path):
        h = _QueuedHarness(tmp_path)
        try:
            h.start(scheduled=True)
            h.supervisor.shutdown(graceful_timeout=0.2)

            assert h.completion_cb.call_count == 1
            completion = h.completion_cb.call_args.args[0]
            assert completion.status == "cancelled"
            assert completion.agent_id == "sa-queued"
            assert h.result_log_cb.call_count == 1
        finally:
            h.release()

    def test_timeout_cancelled_queued_run_suppresses_notification(self, tmp_path):
        h = _QueuedHarness(tmp_path)
        try:
            h.start(scheduled=True)
            h.record._timeout_cancelled = True
            h.supervisor.shutdown(graceful_timeout=0.2)

            assert h.completion_cb.call_count == 0
            assert h.result_log_cb.call_count == 1
            assert h.finish_cb.call_count == 1
        finally:
            h.release()

    def test_on_demand_queued_run_never_notifies(self, tmp_path):
        h = _QueuedHarness(tmp_path)
        try:
            h.start(scheduled=False)
            h.supervisor.shutdown(graceful_timeout=0.2)

            assert h.completion_cb.call_count == 0
            assert h.record.status == "cancelled"
        finally:
            h.release()

    def test_turn_owned_outcome_retained_after_capacity_release(self, tmp_path):
        h = _QueuedHarness(tmp_path)
        try:
            ownership = ExecutionOwnership(origin=ORIGIN_ON_DEMAND, owner_turn_id="r-own")
            h.start(ownership=ownership)
            h.supervisor.shutdown(graceful_timeout=0.2)
            h.supervisor.shutdown(graceful_timeout=0.2)

            assert get_registry().get("sa-queued") is None
            record = h.supervisor.get_owned_record("r-own", "sa-queued")
            assert record is not None and record.result == "[Cancelled]"
            assert h.supervisor.draining_children("r-own") == []
        finally:
            h.release()

    @pytest.mark.parametrize("failing", ["save", "deregister", "close", "grant", "finish", "completion", "result_log"])
    def test_terminal_stage_failure_does_not_skip_later_stages(self, tmp_path, failing):
        h = _QueuedHarness(tmp_path)
        try:
            h.start(scheduled=True, context_key="ctx")
            boom = RuntimeError("boom")
            if failing == "close":
                h.queued.close.side_effect = boom
            elif failing == "grant":
                h.grant_cb.side_effect = boom
            elif failing == "finish":
                h.finish_cb.side_effect = boom
            elif failing == "completion":
                h.completion_cb.side_effect = boom
            elif failing == "result_log":
                h.result_log_cb.side_effect = boom
            patches = []
            if failing == "deregister":
                patches.append(patch("sub_agent_registry.deregister_run", side_effect=boom))
            with ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                if failing == "save":
                    entry = h.supervisor._entries["sa-queued"]
                    entry.request.save_context = MagicMock(side_effect=boom)
                h.supervisor.shutdown(graceful_timeout=0.2)

            assert h.queued.run_calls == 0
            assert h.queued.close.call_count == 1
            assert h.finish_cb.call_count == 1
            assert h.grant_cb.call_count >= 1
            assert h.record._result_event.is_set()
            if failing != "deregister":
                assert get_registry().get("sa-queued") is None
        finally:
            h.release()


class TestShutdownHandoff:
    """Submission/shutdown resolve to rejection-with-rollback or tracked work."""

    @staticmethod
    def _rejected(h, runner):
        assert runner.run_calls == 0
        assert get_registry().get(runner.agent_id) is None

    def test_immediate_worker_completion_retires_entry_baseline(self, tmp_path):
        h = _QueuedHarness(tmp_path)
        runner = FakeRunner(agent_id="sa-fast")
        done = threading.Event()
        h.finish_cb.side_effect = lambda tag: done.set()
        h.supervisor.submit(h.request(runner, scheduled=True), h.options())
        assert _wait_event(done)
        h.supervisor._pool.shutdown(wait=True)

        assert h.supervisor._entries == {}
        assert get_registry().get("sa-fast") is None

    def test_plain_post_shutdown_submission_is_rejected_with_rollback_baseline(self, tmp_path):
        h = _QueuedHarness(tmp_path)
        h.supervisor.shutdown(graceful_timeout=0.1)

        with pytest.raises(RuntimeError):
            h.supervisor.submit(h.request(h.queued, scheduled=True), h.options())

        self._rejected(h, h.queued)
        assert h.queued.close.call_count == 1
        h.finish_cb.assert_not_called()
        assert h.supervisor._entries == {}

    def test_post_shutdown_submission_raises_typed_stopping_error(self, tmp_path):
        from sub_agent_supervisor import SupervisorStoppingError

        h = _QueuedHarness(tmp_path)
        h.supervisor.shutdown(graceful_timeout=0.1)

        with pytest.raises(SupervisorStoppingError) as caught:
            h.supervisor.submit(h.request(h.queued, scheduled=True), h.options())

        assert isinstance(caught.value, RuntimeError)
        self._rejected(h, h.queued)
        assert h.queued.close.call_count == 1
        h.finish_cb.assert_not_called()
        assert h.supervisor._entries == {}

    def test_submission_during_shutdown_drain_is_rejected(self, tmp_path):
        import sub_agent_supervisor as mod

        h = _QueuedHarness(tmp_path)
        in_drain, resume = threading.Event(), threading.Event()
        real_sleep = mod.time.sleep
        shutdown_thread: list[threading.Thread] = []
        calls = []

        def gated_sleep(seconds):
            if threading.current_thread() is shutdown_thread[0] and not calls:
                calls.append(1)
                in_drain.set()
                resume.wait(5.0)
            real_sleep(min(seconds, 0.01))

        try:
            h.supervisor.submit(h.request(h.blocker), h.options())
            assert h.blocker.started.wait(5.0)
            t = threading.Thread(
                target=lambda: h.supervisor.shutdown(graceful_timeout=0.3)
            )
            shutdown_thread.append(t)
            with patch.object(mod.time, "sleep", gated_sleep):
                t.start()
                assert in_drain.wait(5.0)
                try:
                    with pytest.raises(RuntimeError):
                        h.supervisor.submit(
                            h.request(h.queued, scheduled=True), h.options()
                        )
                finally:
                    resume.set()
                t.join(10.0)
            assert not t.is_alive()

            self._rejected(h, h.queued)
            assert h.queued.close.call_count == 1
            assert h.supervisor._entries.get("sa-queued") is None
        finally:
            resume.set()
            h.release()

    def test_submission_paused_after_executor_accept_is_tracked_baseline(self, tmp_path):
        h = _QueuedHarness(tmp_path)
        accepted, go = threading.Event(), threading.Event()
        real_submit = h.supervisor._pool.submit

        def paused_submit(fn, *a, **k):
            future = real_submit(fn, *a, **k)
            if not h.blocker.started.is_set():
                return future
            accepted.set()
            go.wait(5.0)
            return future

        try:
            h.supervisor.submit(h.request(h.blocker), h.options())
            assert h.blocker.started.wait(5.0)
            with patch.object(h.supervisor._pool, "submit", paused_submit):
                t = threading.Thread(
                    target=lambda: h.supervisor.submit(
                        h.request(h.queued, scheduled=True), h.options()
                    )
                )
                t.start()
                assert accepted.wait(5.0)
                reached = threading.Event()
                real_lock = h.supervisor._submit_lock
                shutdown_ident: list[int] = []

                class _ProbeLock:
                    """Signals when the shutdown thread reaches the lock boundary."""

                    def __enter__(self):
                        if shutdown_ident and threading.get_ident() == shutdown_ident[0]:
                            reached.set()
                        return real_lock.__enter__()

                    def __exit__(self, *exc):
                        return real_lock.__exit__(*exc)

                h.supervisor._submit_lock = _ProbeLock()

                def run_shutdown():
                    shutdown_ident.append(threading.get_ident())
                    h.supervisor.shutdown(graceful_timeout=0.2)

                s = threading.Thread(target=run_shutdown)
                s.start()
                assert reached.wait(5.0)  # shutdown is at the handoff boundary
                go.set()
                t.join(10.0)
                s.join(10.0)
            assert not t.is_alive() and not s.is_alive()

            assert h.queued.run_calls == 0
            assert h.queued.close.call_count == 1
            assert h.finish_cb.call_count == 1
            assert get_registry().get("sa-queued") is None
        finally:
            go.set()
            h.release()


class TestShutdownEdges:
    """Reentrancy, caller-context and cooperative-cancellation edges."""

    def test_finish_callback_submission_is_rejected_without_deadlock(self, tmp_path):
        h = _QueuedHarness(tmp_path)
        reentrant = _HoldRunner("sa-reentrant")
        reentrant.close = MagicMock()
        outcome: list[object] = []

        def finish(tag):
            h.events.append("finish")
            try:
                h.supervisor.submit(
                    h.request(reentrant, scheduled=True), h.options()
                )
            except RuntimeError as exc:
                outcome.append(exc)

        h.finish_cb.side_effect = finish
        try:
            h.start(scheduled=True)
            t = threading.Thread(
                target=lambda: h.supervisor.shutdown(graceful_timeout=0.2)
            )
            t.start()
            t.join(10.0)
            assert not t.is_alive()

            assert len(outcome) == 1
            assert reentrant.run_calls == 0
            assert reentrant.close.call_count == 1
            assert get_registry().get("sa-reentrant") is None
            assert h.queued.close.call_count == 1
        finally:
            h.release()

    def test_shutdown_preserves_callers_run_context(self, tmp_path):
        import structlog

        from agent_logging import bind_run_context, clear_run_context

        h = _QueuedHarness(tmp_path)
        try:
            h.start(scheduled=True)
            bind_run_context(trace="r-caller", agent="main")
            h.supervisor.shutdown(graceful_timeout=0.2)

            bound = structlog.contextvars.get_contextvars()
            assert bound.get("trace") == "r-caller"
            assert bound.get("agent") == "main"
        finally:
            clear_run_context()
            h.release()

    def test_running_runner_cancelled_cooperatively_closes_after_unwind(self, tmp_path):
        class CoopRunner(FakeRunner):
            def __init__(self):
                super().__init__(agent_id="sa-coop")
                self.started = threading.Event()
                self.closed_at_unwind = None

            def run(self, task):
                self.started.set()
                self._cancel_event.wait(5.0)
                self.closed_at_unwind = self.closed
                return "[Cancelled]"

        h = _QueuedHarness(tmp_path)
        coop = CoopRunner()
        done = threading.Event()
        h.finish_cb.side_effect = lambda tag: done.set()
        h.supervisor.submit(h.request(coop, scheduled=True), h.options())
        assert coop.started.wait(5.0)
        h.supervisor.shutdown(graceful_timeout=5.0)
        assert _wait_event(done)

        assert coop.closed_at_unwind is False
        assert coop.closed is True
        assert h.supervisor._entries == {}
        h.supervisor._pool.shutdown(wait=True)


class TestCancellationIsolation:
    """Cancellation-derived BaseExceptions never skip terminal stages or entries."""

    @staticmethod
    def _shutdown_expecting_cancel(h):
        import asyncio

        with pytest.raises(asyncio.CancelledError):
            h.supervisor.shutdown(graceful_timeout=0.2)

    @staticmethod
    def _assert_terminal(h, runner, record):
        assert runner.run_calls == 0
        assert record._result_event.is_set()
        assert runner.close.call_count == 1
        assert get_registry().get(runner.agent_id) is None

    def test_cancellation_in_context_save_still_signals_and_cleans(self, tmp_path):
        import asyncio

        h = _QueuedHarness(tmp_path)
        try:
            h.start(scheduled=True, context_key="ctx")
            entry = h.supervisor._entries["sa-queued"]
            entry.request.save_context = MagicMock(side_effect=asyncio.CancelledError())
            self._shutdown_expecting_cancel(h)

            self._assert_terminal(h, h.queued, h.record)
            assert h.result_log_cb.call_count == 1
            assert h.completion_cb.call_count == 1
            assert h.grant_cb.call_count >= 1
            assert h.finish_cb.call_count == 1
        finally:
            h.release()

    @pytest.mark.parametrize("stage", ["close", "grant"])
    def test_cancellation_in_cleanup_stage_still_runs_later_stages(self, tmp_path, stage):
        import asyncio

        h = _QueuedHarness(tmp_path)
        try:
            h.start(scheduled=True)
            if stage == "close":
                h.queued.close.side_effect = asyncio.CancelledError()
            else:
                h.grant_cb.side_effect = asyncio.CancelledError()
            self._shutdown_expecting_cancel(h)

            self._assert_terminal(h, h.queued, h.record)
            assert h.grant_cb.call_count >= 1
            assert h.finish_cb.call_count == 1
            assert "sa-queued" not in h.supervisor._entries
        finally:
            h.release()

    @staticmethod
    def _two_queued(h):
        """Blocker plus two queued accepted runners; returns the second one."""
        second = _HoldRunner("sa-queued2")
        second.close = MagicMock(side_effect=lambda: h.events.append("close2"))
        h.start(scheduled=True)
        h.supervisor.submit(h.request(second, scheduled=True), h.options())
        return second

    def test_cancellation_in_first_entry_does_not_abandon_later_entries(self, tmp_path):
        import asyncio

        h = _QueuedHarness(tmp_path)
        try:
            second = self._two_queued(h)
            h.queued.close.side_effect = asyncio.CancelledError()
            self._shutdown_expecting_cancel(h)

            assert second.run_calls == 0
            assert second.close.call_count == 1
            assert get_registry().get("sa-queued2") is None
            assert get_registry().get("sa-queued") is None
            assert h.finish_cb.call_count == 2
            assert {"sa-queued", "sa-queued2"}.isdisjoint(h.supervisor._entries)
        finally:
            h.release()

    def test_owned_signal_failure_is_contained_and_entries_retire(self, tmp_path):
        h = _QueuedHarness(tmp_path)
        try:
            second = self._two_queued(h)
            real = h.supervisor._signal_owned_cleanup
            calls = []

            def flaky(record, errors=None):
                calls.append(record.agent_id)
                if record.agent_id == "sa-queued":
                    raise RuntimeError("owned signal boom")
                return real(record, errors)

            with patch.object(h.supervisor, "_signal_owned_cleanup", flaky):
                h.supervisor.shutdown(graceful_timeout=0.2)

            assert second.close.call_count == 1
            assert {"sa-queued", "sa-queued2"} <= set(calls)
            assert {"sa-queued", "sa-queued2"}.isdisjoint(h.supervisor._entries)
            assert h.finish_cb.call_count == 2
        finally:
            h.release()

    @pytest.mark.parametrize("hook", ["result_log", "completion", "finish"])
    def test_cancellation_in_callback_keeps_other_attempts_and_entries(self, tmp_path, hook):
        import asyncio

        h = _QueuedHarness(tmp_path)
        try:
            second = self._two_queued(h)
            cb = {"result_log": h.result_log_cb, "completion": h.completion_cb,
                  "finish": h.finish_cb}[hook]
            first_call = []

            def once(*a, **k):
                if not first_call:
                    first_call.append(1)
                    raise asyncio.CancelledError()

            cb.side_effect = once
            self._shutdown_expecting_cancel(h)

            assert second.close.call_count == 1
            assert h.queued.close.call_count == 1
            assert h.record._result_event.is_set()
            assert h.finish_cb.call_count == 2
            assert h.completion_cb.call_count == 2
            assert h.result_log_cb.call_count == 2
            assert {"sa-queued", "sa-queued2"}.isdisjoint(h.supervisor._entries)
        finally:
            h.release()


class TestPrimaryCancellationPreserved:
    """Terminal cleanup never replaces or swallows an in-flight cancellation."""

    def test_context_cancel_plus_owned_signal_failure_keeps_original(self, tmp_path):
        import asyncio

        h = _QueuedHarness(tmp_path)
        primary = asyncio.CancelledError("primary")
        try:
            h.start(scheduled=True, context_key="ctx")
            h.supervisor._entries["sa-queued"].request.save_context = MagicMock(
                side_effect=primary
            )
            with patch.object(
                h.supervisor, "_signal_owned_cleanup",
                side_effect=RuntimeError("owned signal boom"),
            ):
                with pytest.raises(BaseException) as caught:
                    h.supervisor.shutdown(graceful_timeout=0.2)

            assert caught.value is primary
            assert "sa-queued" not in h.supervisor._entries
            assert h.finish_cb.call_count == 1
        finally:
            h.release()

    @pytest.mark.parametrize("stage", ["close", "owned_signal"])
    def test_cleanup_cancellation_does_not_replace_terminal_primary(self, tmp_path, stage):
        import asyncio

        h = _QueuedHarness(tmp_path)
        primary = asyncio.CancelledError("primary")
        secondary = asyncio.CancelledError("secondary")
        try:
            h.start(scheduled=True, context_key="ctx")
            h.supervisor._entries["sa-queued"].request.save_context = MagicMock(
                side_effect=primary
            )
            if stage == "close":
                h.queued.close.side_effect = secondary
                ctx = contextlib.nullcontext()
            else:
                ctx = patch.object(
                    h.supervisor, "_signal_owned_cleanup", side_effect=secondary
                )
            with ctx:
                with pytest.raises(BaseException) as caught:
                    h.supervisor.shutdown(graceful_timeout=0.2)

            assert caught.value is primary
            assert h.finish_cb.call_count == 1
            assert "sa-queued" not in h.supervisor._entries
        finally:
            h.release()

    def test_execution_cancel_plus_cleanup_cancel_keeps_execution_primary(self, tmp_path):
        import asyncio

        h = _QueuedHarness(tmp_path)
        primary = asyncio.CancelledError("exec primary")
        runner = FakeRunner(agent_id="sa-exec")
        runner.run = MagicMock(side_effect=primary)
        runner.close = MagicMock(side_effect=asyncio.CancelledError("cleanup"))
        request, options = h.request(runner, scheduled=True), h.options()
        with patch.object(h.supervisor._pool, "submit", return_value=MagicMock()):
            h.supervisor.submit(request, options)
        record = get_registry().get("sa-exec")
        try:
            with pytest.raises(BaseException) as caught:
                h.supervisor._run_and_notify(request, options, runner, record)

            assert caught.value is primary
            assert h.finish_cb.call_count == 1
            assert "sa-exec" not in h.supervisor._entries
        finally:
            h.supervisor._pool.shutdown(wait=True)


class TestExplicitPrimaryCleanup:
    """Cleanup decides cancellation from an explicit primary, never ambient state."""

    @staticmethod
    def _run_worker(h, runner):
        request, options = h.request(runner, scheduled=True), h.options()
        with patch.object(h.supervisor._pool, "submit", return_value=MagicMock()):
            h.supervisor.submit(request, options)
        record = get_registry().get(runner.agent_id)
        return request, options, record

    def test_success_inside_unrelated_outer_except_propagates_cleanup_cancel(
        self, tmp_path
    ):
        import asyncio

        h = _QueuedHarness(tmp_path)
        cancel = asyncio.CancelledError("cleanup")
        runner = FakeRunner(agent_id="sa-ambient")
        runner.close = MagicMock(side_effect=cancel)
        request, options, record = self._run_worker(h, runner)
        try:
            try:
                raise ValueError("unrelated outer handler")
            except ValueError:
                with pytest.raises(BaseException) as caught:
                    h.supervisor._run_and_notify(request, options, runner, record)

            assert caught.value is cancel
            assert h.finish_cb.call_count == 1
            assert "sa-ambient" not in h.supervisor._entries
        finally:
            h.supervisor._pool.shutdown(wait=True)

    def test_handled_execution_failure_does_not_suppress_cleanup_cancel(self, tmp_path):
        import asyncio

        h = _QueuedHarness(tmp_path)
        cancel = asyncio.CancelledError("cleanup")
        runner = FakeRunner(agent_id="sa-handled")
        runner.run = MagicMock(side_effect=RuntimeError("handled boom"))
        runner.close = MagicMock(side_effect=cancel)
        request, options, record = self._run_worker(h, runner)
        try:
            with pytest.raises(BaseException) as caught:
                h.supervisor._run_and_notify(request, options, runner, record)

            assert caught.value is cancel
            assert record.status == "failed"
            assert h.finish_cb.call_count == 1
            assert "sa-handled" not in h.supervisor._entries
        finally:
            h.supervisor._pool.shutdown(wait=True)


class TestTerminalPrecedence:
    """Terminal effects are independent attempts with deterministic precedence."""

    @staticmethod
    def _worker(h, runner, context_key=None):
        request = h.request(runner, scheduled=True)
        if context_key:
            request.context_key = context_key
        options = h.options()
        with patch.object(h.supervisor._pool, "submit", return_value=MagicMock()):
            h.supervisor.submit(request, options)
        return request, options, get_registry().get(runner.agent_id)

    def test_context_cancel_plus_signal_error_still_attempts_log_and_completion(
        self, tmp_path
    ):
        import asyncio

        h = _QueuedHarness(tmp_path)
        cancel = asyncio.CancelledError("context save")
        runner = FakeRunner(agent_id="sa-pair1")
        request, options, record = self._worker(h, runner, context_key="ctx")
        request.save_context = MagicMock(side_effect=cancel)
        record.signal_result = MagicMock(side_effect=RuntimeError("signal boom"))
        try:
            with patch("sub_agent_supervisor.logger.info") as info, \
                    pytest.raises(BaseException) as caught:
                h.supervisor._run_and_notify(request, options, runner, record)

            assert caught.value is cancel
            assert record.signal_result.call_count == 1
            lifecycle = [c for c in info.call_args_list if "done |" in c.args[0]]
            assert len(lifecycle) == 1
            assert h.result_log_cb.call_count == 1
            assert h.completion_cb.call_count == 1
            assert h.finish_cb.call_count == 1
            assert "sa-pair1" not in h.supervisor._entries
        finally:
            h.supervisor._pool.shutdown(wait=True)

    def test_cancel_after_earlier_ordinary_failure_wins_and_finalizes_once(
        self, tmp_path
    ):
        import asyncio

        h = _QueuedHarness(tmp_path)
        cancel = asyncio.CancelledError("result log")
        runner = FakeRunner(agent_id="sa-pair2")
        request, options, record = self._worker(h, runner)
        record.signal_result = MagicMock(side_effect=RuntimeError("signal boom"))
        h.result_log_cb.side_effect = cancel
        try:
            with pytest.raises(BaseException) as caught:
                h.supervisor._run_and_notify(request, options, runner, record)

            assert caught.value is cancel
            assert record.signal_result.call_count == 1
            assert h.result_log_cb.call_count == 1
            assert h.completion_cb.call_count == 1
            assert h.finish_cb.call_count == 1
        finally:
            h.supervisor._pool.shutdown(wait=True)

    def test_ordinary_terminal_failure_propagates_without_second_finalize(
        self, tmp_path
    ):
        h = _QueuedHarness(tmp_path)
        boom = RuntimeError("signal boom")
        runner = FakeRunner(agent_id="sa-once")
        request, options, record = self._worker(h, runner)
        record.signal_result = MagicMock(side_effect=boom)
        try:
            with pytest.raises(RuntimeError) as caught:
                h.supervisor._run_and_notify(request, options, runner, record)

            assert caught.value is boom
            assert record.signal_result.call_count == 1
            assert h.result_log_cb.call_count == 1
            assert h.completion_cb.call_count == 1
            assert record.status == "done"
            assert h.finish_cb.call_count == 1
        finally:
            h.supervisor._pool.shutdown(wait=True)

    def test_completion_cancel_after_signal_error_wins(self, tmp_path):
        import asyncio

        h = _QueuedHarness(tmp_path)
        cancel = asyncio.CancelledError("completion")
        runner = FakeRunner(agent_id="sa-comp1")
        request, options, record = self._worker(h, runner)
        record.signal_result = MagicMock(side_effect=RuntimeError("signal boom"))
        h.completion_cb.side_effect = cancel
        try:
            with pytest.raises(BaseException) as caught:
                h.supervisor._run_and_notify(request, options, runner, record)

            assert caught.value is cancel
            assert record.signal_result.call_count == 1
            assert h.completion_cb.call_count == 1
            assert h.finish_cb.call_count == 1
        finally:
            h.supervisor._pool.shutdown(wait=True)

    def test_first_cancel_wins_over_later_completion_cancel(self, tmp_path):
        import asyncio

        h = _QueuedHarness(tmp_path)
        first = asyncio.CancelledError("context first")
        runner = FakeRunner(agent_id="sa-comp2")
        request, options, record = self._worker(h, runner, context_key="ctx")
        request.save_context = MagicMock(side_effect=first)
        h.completion_cb.side_effect = asyncio.CancelledError("completion later")
        try:
            with pytest.raises(BaseException) as caught:
                h.supervisor._run_and_notify(request, options, runner, record)

            assert caught.value is first
            assert h.completion_cb.call_count == 1
            assert h.finish_cb.call_count == 1
        finally:
            h.supervisor._pool.shutdown(wait=True)


class TestCancelledEntryExplicitPrimary:
    """Shutdown-side cancelled-entry cleanup also ignores ambient handler state."""

    def test_unrelated_outer_except_does_not_suppress_cleanup_cancel(self, tmp_path):
        import asyncio

        h = _QueuedHarness(tmp_path)
        cancel = asyncio.CancelledError("close")
        try:
            h.start(scheduled=True)
            h.queued.close.side_effect = cancel
            entry = h.supervisor._entries["sa-queued"]
            try:
                raise ValueError("unrelated outer handler")
            except ValueError:
                with pytest.raises(BaseException) as caught:
                    h.supervisor._finalize_cancelled_entry(entry)

            assert caught.value is cancel
            assert h.finish_cb.call_count == 1
            assert "sa-queued" not in h.supervisor._entries
        finally:
            h.release()


class TestQueueFirstShutdown:
    """Queued-work finalization precedes the cooperative started-work drain."""

    def test_queued_entry_finalized_before_worker_drain_and_started_client_open(
        self, tmp_path
    ):
        import sub_agent_supervisor as mod

        h = _QueuedHarness(tmp_path)
        in_drain, resume = threading.Event(), threading.Event()
        snapshot: dict = {}
        real_sleep = mod.time.sleep
        shutdown_thread: list[threading.Thread] = []
        blocker_close = MagicMock()
        h.blocker.close = blocker_close

        def gated_sleep(seconds):
            if threading.current_thread() is shutdown_thread[0] and not snapshot:
                snapshot["events"] = list(h.events)
                snapshot["blocker_closed"] = blocker_close.call_count
                in_drain.set()
                resume.wait(5.0)
            real_sleep(min(seconds, 0.01))

        try:
            h.start(scheduled=True)
            t = threading.Thread(
                target=lambda: h.supervisor.shutdown(graceful_timeout=0.3)
            )
            shutdown_thread.append(t)
            with patch.object(mod.time, "sleep", gated_sleep):
                t.start()
                assert in_drain.wait(5.0)
                resume.set()
                t.join(10.0)
            assert not t.is_alive()

            assert "close" in snapshot["events"]
            assert "finish" in snapshot["events"]
            assert snapshot["blocker_closed"] == 0
            assert h.queued.run_calls == 0
        finally:
            resume.set()
            h.release()

    @staticmethod
    def _two_queued(h):
        second = _HoldRunner("sa-queued2")
        second.close = MagicMock(side_effect=lambda: h.events.append("close2"))
        h.start(scheduled=True)
        h.supervisor.submit(h.request(second, scheduled=True), h.options())
        return second

    def test_first_cleanup_cancel_outranks_later_drain_failure_and_attempts_all(
        self, tmp_path
    ):
        import asyncio
        import sub_agent_registry

        h = _QueuedHarness(tmp_path)
        cancel = asyncio.CancelledError("first cleanup")
        second = _HoldRunner("sa-queued2")
        second.close = MagicMock(side_effect=lambda: h.events.append("close2"))
        per_entry: dict[str, dict[str, MagicMock]] = {}

        def entry_options(agent_id):
            obs = {
                name: MagicMock()
                for name in ("finish", "grant", "completion", "result_log")
            }
            per_entry[agent_id] = obs
            return SupervisionOptions(
                notify=True, finish_cb=obs["finish"], grant_scope_cb=obs["grant"],
                completion_cb=obs["completion"], result_log_cb=obs["result_log"],
            )

        signals: list[str] = []
        real_signal = h.supervisor._signal_owned_cleanup

        def spy_signal(record, errors=None):
            signals.append(record.agent_id)
            return real_signal(record, errors)

        try:
            h.supervisor.submit(h.request(h.blocker), h.options())
            assert h.blocker.started.wait(5.0)
            h.supervisor.submit(
                h.request(h.queued, scheduled=True, context_key="ctx1"),
                entry_options("sa-queued"),
            )
            h.supervisor.submit(
                h.request(second, scheduled=True, context_key="ctx2"),
                entry_options("sa-queued2"),
            )
            h.queued.close.side_effect = cancel
            records = {
                agent_id: get_registry().get(agent_id)
                for agent_id in ("sa-queued", "sa-queued2")
            }
            signal_spies = {}
            for agent_id, record in records.items():
                signal_spies[agent_id] = MagicMock(wraps=record.signal_result)
                record.signal_result = signal_spies[agent_id]
            registry = sub_agent_registry.get_registry()
            calls = {"n": 0}
            real_list = registry.list_active

            def flaky_list():
                calls["n"] += 1
                if calls["n"] > 1:  # first call is shutdown's own snapshot
                    raise RuntimeError("drain boom")
                return real_list()

            with patch.object(registry, "list_active", flaky_list), \
                    patch.object(h.supervisor, "_signal_owned_cleanup", spy_signal):
                with pytest.raises(BaseException) as caught:
                    h.supervisor.shutdown(graceful_timeout=0.2)

            assert caught.value is cancel
            assert second.close.call_count == 1
            assert h.queued.close.call_count == 1
            assert calls["n"] >= 2  # drain was attempted despite the cancellation
            assert {"sa-queued", "sa-queued2"}.isdisjoint(h.supervisor._entries)
            for agent_id in ("sa-queued", "sa-queued2"):
                obs = per_entry[agent_id]
                assert obs["result_log"].call_count == 1, agent_id
                assert obs["completion"].call_count == 1, agent_id
                assert obs["finish"].call_count == 1, agent_id
                assert obs["grant"].call_count == 1, agent_id
                assert signals.count(agent_id) == 1, agent_id
                signal_spies[agent_id].assert_called_once_with()  # real signal ran
                assert records[agent_id]._result_event.is_set(), agent_id
            assert sorted(key for key, _ in h.saved) == ["ctx1", "ctx2"]
            assert not any(signalled for _, signalled in h.saved)  # saved before signal
            h.finish_cb.assert_not_called()  # blocker (started work) not finalized
        finally:
            h.release()

    def test_ordinary_drain_failure_propagates_after_every_entry_finalized(
        self, tmp_path
    ):
        import sub_agent_registry

        h = _QueuedHarness(tmp_path)
        boom = RuntimeError("drain boom")
        try:
            second = self._two_queued(h)
            registry = sub_agent_registry.get_registry()
            real_list = registry.list_active
            calls = {"n": 0}

            def flaky_list():
                calls["n"] += 1
                if calls["n"] > 1:
                    raise boom
                return real_list()

            with patch.object(registry, "list_active", flaky_list):
                with pytest.raises(RuntimeError) as caught:
                    h.supervisor.shutdown(graceful_timeout=0.2)

            assert caught.value is boom
            assert second.close.call_count == 1
            assert h.queued.close.call_count == 1
            assert h.finish_cb.call_count == 2
        finally:
            h.release()

    def test_shutdown_in_unrelated_handler_does_not_suppress_cleanup_cancel(
        self, tmp_path
    ):
        import asyncio

        h = _QueuedHarness(tmp_path)
        cancel = asyncio.CancelledError("cleanup")
        try:
            h.start(scheduled=True)
            h.queued.close.side_effect = cancel
            try:
                raise ValueError("unrelated outer handler")
            except ValueError:
                with pytest.raises(BaseException) as caught:
                    h.supervisor.shutdown(graceful_timeout=0.2)

            assert caught.value is cancel
            assert h.finish_cb.call_count == 1
        finally:
            h.release()

    def test_repeated_shutdown_does_not_duplicate_queued_effects(self, tmp_path):
        h = _QueuedHarness(tmp_path)
        try:
            h.start(scheduled=True)
            h.supervisor.shutdown(graceful_timeout=0.2)
            h.supervisor.shutdown(graceful_timeout=0.2)

            assert h.queued.close.call_count == 1
            assert h.finish_cb.call_count == 1
            assert h.completion_cb.call_count == 1
        finally:
            h.release()

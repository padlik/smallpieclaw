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

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from memory_store import ShortTermMemory
from sub_agent_registry import SubAgentRegistry, get_registry
from sub_agent_supervisor import (
    SubAgentSupervisor,
    SubmissionRequest,
    SupervisionOptions,
)


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
        mock_executor.spawn_agent = MagicMock(return_value={
            "success": False,
            "output": "",
            "error": "spawn_agent: max_subagents cap reached (6/6).",
            "exit_code": -1,
        })
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
                notify=False, expandable=False,
            ),
        )
        res_b = exc._exec_spawn_agent(
            {"task": "task B", "context_payload": {"note": "b"}},
            caller_depth=0,
            options=SupervisionOptions(
                job_tag="job-b", finish_cb=finish_cb, result_log_cb=result_log,
                notify=False, expandable=False,
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
        )
        assert res["success"]
        record = get_registry().get(res["agent_id"])
        assert record is not None
        return exc, runner, record

    def test_timeout_cancel_suppresses_stale_notification(self, make_builtin_executor, tmp_path):
        exc, runner, record = self._spawn_blocking(make_builtin_executor, tmp_path, "sa-stale")

        # get_agent_result times out and auto-cancels the run.
        out = exc._exec_get_agent_result({"agent_id": record.agent_id, "timeout": 0})
        assert out["status"] == "timeout"
        assert record._timeout_cancelled is True

        assert _wait_event(record._result_event)
        assert record.status == "cancelled"
        # The timed-out run must NOT emit a stale cancellation notification.
        assert runner.notify_calls == []

    def test_manual_cancel_still_notifies(self, make_builtin_executor, tmp_path):
        """Contrast: a non-timeout cancel is not suppressed (flag stays False)."""
        exc, runner, record = self._spawn_blocking(make_builtin_executor, tmp_path, "sa-manual")

        record.cancel()  # e.g. operator /agents cancel — not a get_agent_result timeout
        assert record._timeout_cancelled is False

        assert _wait_event(record._result_event)
        assert record.status == "cancelled"
        assert len(runner.notify_calls) == 1
        assert "cancelled" in runner.notify_calls[0].lower()


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
                    expandable=False,
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
                          side_effect=lambda fn, *a, **kw: fn()):
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
        assert "not found" in res["error"]
        # finish_cb must NOT fire on synchronous admission failure.
        assert finish_calls == []


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

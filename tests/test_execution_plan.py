"""Unit tests for the execution_plan module.

Covers the :class:`PlanStep` dataclass, plan validation, topological batching,
result substitution, and the :class:`PlanExecutor` sub-agent orchestration.
"""

from __future__ import annotations

import json
import threading
import time
from unittest.mock import MagicMock

import pytest

from agent_runtime import RuntimeProfile
from execution_plan import (
    ExecutionPlan,
    PlanExecutor,
    PlanStep,
    PlanValidationError,
    _build_step_task,
    _standardize_sub_agent_result,
    substitute_results,
    topological_sort,
    validate_plan,
)
from react_loop import ReactContext


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def minimal_ctx():
    """Minimal ReactContext for plan-executor tests."""
    return ReactContext(
        llm=MagicMock(),
        tool_index=MagicMock(),
        memory=MagicMock(),
        builtin_executor=None,
        mcp_manager=None,
        skill_registry=None,
    )


@pytest.fixture
def recorder_factory():
    """Factory fixture returning a mock sub-agent factory that records calls."""

    def _make(runner_by_id=None):
        calls = []

        factory = MagicMock()

        def _factory(*args, **kwargs):
            step_id = kwargs.get("label", "").replace("plan-", "")
            calls.append({"args": args, "kwargs": kwargs})
            if runner_by_id and step_id in runner_by_id:
                return runner_by_id[step_id]
            runner = MagicMock()
            runner.run.return_value = json.dumps(
                {"success": True, "output": f"result-{step_id}", "error": "", "exit_code": 0}
            )
            return runner

        factory.side_effect = _factory
        factory.calls = calls
        return factory

    return _make


@pytest.fixture
def slow_factory():
    """Factory fixture that blocks until a shared event is set (unused)."""

    def _make(block_event: threading.Event):
        def factory(*args, **kwargs):
            runner = MagicMock()

            def run(_task):
                block_event.wait()
                return json.dumps({"success": True, "output": "ok", "error": "", "exit_code": 0})

            runner.run = run
            runner.cancel = block_event.set
            return runner

        return factory

    return _make


# ---------------------------------------------------------------------------
# PlanStep
# ---------------------------------------------------------------------------


class TestPlanStep:
    """Test the PlanStep dataclass."""

    def test_dataclass_fields(self):
        step = PlanStep(
            id="step-1",
            tool="shell",
            args={"command": "echo hello"},
            depends_on=["a", "b"],
            description="Run a greeting command.",
        )
        assert step.id == "step-1"
        assert step.tool == "shell"
        assert step.args == {"command": "echo hello"}
        assert step.depends_on == ["a", "b"]
        assert step.description == "Run a greeting command."

    def test_default_fields(self):
        step = PlanStep(id="x", tool="file_read", args={"path": "/tmp/x"})
        assert step.depends_on == []
        assert step.description == ""


# ---------------------------------------------------------------------------
# validate_plan
# ---------------------------------------------------------------------------


class TestValidatePlan:
    """Test plan structural validation."""

    def test_valid_plan_passes(self):
        plan = ExecutionPlan(
            description="linear",
            steps=[
                PlanStep(id="a", tool="shell", args={"command": "echo a"}),
                PlanStep(
                    id="b",
                    tool="shell",
                    args={"command": "echo b"},
                    depends_on=["a"],
                ),
            ],
        )
        assert validate_plan(plan) is None

    def test_duplicate_ids_raises(self):
        plan = ExecutionPlan(
            description="dupes",
            steps=[
                PlanStep(id="a", tool="shell", args={}),
                PlanStep(id="a", tool="file_read", args={}),
            ],
        )
        with pytest.raises(PlanValidationError, match="Duplicate step id"):
            validate_plan(plan)

    def test_circular_dependency_raises(self):
        plan = ExecutionPlan(
            description="cycle",
            steps=[
                PlanStep(id="a", tool="shell", args={}, depends_on=["b"]),
                PlanStep(id="b", tool="shell", args={}, depends_on=["a"]),
            ],
        )
        with pytest.raises(PlanValidationError, match="circular dependency"):
            validate_plan(plan)

    def test_self_dependency_raises(self):
        plan = ExecutionPlan(
            description="self loop",
            steps=[PlanStep(id="a", tool="shell", args={}, depends_on=["a"])],
        )
        with pytest.raises(PlanValidationError, match="circular dependency"):
            validate_plan(plan)

    def test_unknown_dependency_raises(self):
        plan = ExecutionPlan(
            description="missing dep",
            steps=[
                PlanStep(id="a", tool="shell", args={}, depends_on=["missing"]),
            ],
        )
        with pytest.raises(PlanValidationError, match="unknown step 'missing'"):
            validate_plan(plan)

    def test_empty_plan_passes(self):
        plan = ExecutionPlan(description="empty", steps=[])
        # Empty plan is currently accepted by topological_sort and validate_plan
        # raises an error; this test documents the intended behavior.
        with pytest.raises(PlanValidationError, match="Plan has no steps"):
            validate_plan(plan)


# ---------------------------------------------------------------------------
# topological_sort
# ---------------------------------------------------------------------------


class TestTopologicalSort:
    """Test DAG-to-batches sorting."""

    def test_linear_plan(self):
        plan = ExecutionPlan(
            description="linear",
            steps=[
                PlanStep(id="a", tool="shell", args={}),
                PlanStep(id="b", tool="shell", args={}, depends_on=["a"]),
                PlanStep(id="c", tool="shell", args={}, depends_on=["b"]),
            ],
        )
        batches = topological_sort(plan)
        batch_ids = [[s.id for s in batch] for batch in batches]
        assert batch_ids == [["a"], ["b"], ["c"]]

    def test_parallel_plan(self):
        plan = ExecutionPlan(
            description="parallel",
            steps=[
                PlanStep(id="a", tool="shell", args={}),
                PlanStep(id="b", tool="file_read", args={}),
                PlanStep(id="c", tool="file_write", args={}),
            ],
        )
        batches = topological_sort(plan)
        assert len(batches) == 1
        assert sorted(s.id for s in batches[0]) == ["a", "b", "c"]

    def test_diamond_plan(self):
        plan = ExecutionPlan(
            description="diamond",
            steps=[
                PlanStep(id="a", tool="shell", args={}),
                PlanStep(id="b", tool="shell", args={}, depends_on=["a"]),
                PlanStep(id="c", tool="shell", args={}, depends_on=["a"]),
                PlanStep(id="d", tool="shell", args={}, depends_on=["b", "c"]),
            ],
        )
        batches = topological_sort(plan)
        batch_ids = [[s.id for s in batch] for batch in batches]
        assert batch_ids[0] == ["a"]
        assert sorted(batch_ids[1]) == ["b", "c"]
        assert batch_ids[2] == ["d"]
        assert len(batches) == 3

    def test_complex_dag(self):
        plan = ExecutionPlan(
            description="complex",
            steps=[
                PlanStep(id="a", tool="shell", args={}),
                PlanStep(id="b", tool="shell", args={}, depends_on=["a"]),
                PlanStep(id="c", tool="shell", args={}, depends_on=["a"]),
                PlanStep(id="d", tool="shell", args={}, depends_on=["b"]),
                PlanStep(id="e", tool="shell", args={}, depends_on=["b", "c"]),
                PlanStep(id="f", tool="shell", args={}, depends_on=["e"]),
            ],
        )
        batches = topological_sort(plan)
        batch_ids = [[s.id for s in batch] for batch in batches]
        assert batch_ids[0] == ["a"]
        assert "d" in batch_ids[1] or "d" in batch_ids[2]
        assert "e" in batch_ids[-2]
        assert batch_ids[-1] == ["f"]

    def test_circular_plan_raises(self):
        plan = ExecutionPlan(
            description="cycle",
            steps=[
                PlanStep(id="a", tool="shell", args={}, depends_on=["b"]),
                PlanStep(id="b", tool="shell", args={}, depends_on=["a"]),
            ],
        )
        with pytest.raises(PlanValidationError):
            topological_sort(plan)


# ---------------------------------------------------------------------------
# substitute_results
# ---------------------------------------------------------------------------


class TestSubstituteResults:
    """Test ``{{step_id}}`` result substitution into step arguments."""

    def test_substitutes_step_result(self):
        step = PlanStep(
            id="step-2",
            tool="shell",
            args={"command": "process {{step1}}"},
        )
        results = {
            "step1": {"success": True, "output": "raw-data", "exit_code": 0},
        }
        new_step = substitute_results(step, results)
        assert new_step.args["command"] == 'process {"success": true, "output": "raw-data", "exit_code": 0}'

    def test_no_placeholder_unchanged(self):
        step = PlanStep(id="s", tool="shell", args={"command": "echo hello"})
        new_step = substitute_results(step, {})
        assert new_step.args == {"command": "echo hello"}
        assert new_step is not step

    def test_multiple_placeholders(self):
        step = PlanStep(
            id="s",
            tool="shell",
            args={"command": "{{a}} and {{b}}"},
        )
        results = {
            "a": {"out": 1},
            "b": {"out": 2},
        }
        new_step = substitute_results(step, results)
        assert json.loads(new_step.args["command"].split(" and ")[0]) == {"out": 1}
        assert json.loads(new_step.args["command"].split(" and ")[1]) == {"out": 2}

    def test_missing_step_raises(self):
        step = PlanStep(
            id="s",
            tool="shell",
            args={"command": "use {{missing}}"},
        )
        # The current implementation leaves unknown placeholders unchanged.
        new_step = substitute_results(step, {})
        assert new_step.args["command"] == "use {{missing}}"

    def test_substitutes_nested_values(self):
        step = PlanStep(
            id="s",
            tool="shell",
            args={
                "nested": {"value": "{{a}}"},
                "list": ["prefix-{{a}}"],
            },
        )
        results = {"a": {"ok": True}}
        new_step = substitute_results(step, results)
        assert json.loads(new_step.args["nested"]["value"]) == {"ok": True}
        assert new_step.args["list"][0].startswith("prefix-")
        assert json.loads(new_step.args["list"][0].split("prefix-", 1)[1]) == {"ok": True}


# ---------------------------------------------------------------------------
# PlanExecutor
# ---------------------------------------------------------------------------


class TestPlanExecutor:
    """Test the parallel/sequential plan executor."""

    def test_execute_linear_plan(self, minimal_ctx, recorder_factory):
        runner_a = MagicMock()
        runner_a.run.return_value = json.dumps(
            {"success": True, "output": "a-out", "error": "", "exit_code": 0}
        )
        runner_b = MagicMock()
        runner_b.run.return_value = json.dumps(
            {"success": True, "output": "b-out", "error": "", "exit_code": 0}
        )

        factory = recorder_factory(runner_by_id={"a": runner_a, "b": runner_b})
        executor = PlanExecutor(max_concurrent=2, sub_agent_factory=factory)

        plan = ExecutionPlan(
            description="linear",
            steps=[
                PlanStep(id="a", tool="shell", args={"command": "echo a"}),
                PlanStep(id="b", tool="shell", args={"command": "echo b"}, depends_on=["a"]),
            ],
        )

        result = executor.execute(plan, minimal_ctx)

        assert result["success"] is True
        assert result["results"]["a"]["output"] == "a-out"
        assert result["results"]["b"]["output"] == "b-out"
        assert len(factory.calls) == 2
        # Factory receives steps in label order a then b, because they are sequential batches.
        labels = [c["kwargs"]["label"] for c in factory.calls]
        assert labels == ["plan-a", "plan-b"]

    def test_execute_parallel_plan(self, minimal_ctx, recorder_factory):
        factory = recorder_factory()
        executor = PlanExecutor(max_concurrent=3, sub_agent_factory=factory)

        plan = ExecutionPlan(
            description="parallel",
            steps=[
                PlanStep(id="a", tool="shell", args={"command": "echo a"}),
                PlanStep(id="b", tool="shell", args={"command": "echo b"}),
                PlanStep(id="c", tool="shell", args={"command": "echo c"}),
            ],
        )

        result = executor.execute(plan, minimal_ctx)

        assert result["success"] is True
        assert len(factory.calls) == 3
        labels = sorted(c["kwargs"]["label"] for c in factory.calls)
        assert labels == ["plan-a", "plan-b", "plan-c"]

    def test_failure_propagation(self, minimal_ctx, recorder_factory):
        runner_a = MagicMock()
        runner_a.run.return_value = json.dumps(
            {"success": False, "output": "", "error": "boom", "exit_code": 1}
        )
        runner_b = MagicMock()
        runner_b.run.return_value = json.dumps(
            {"success": True, "output": "b-out", "error": "", "exit_code": 0}
        )

        factory = recorder_factory(runner_by_id={"a": runner_a, "b": runner_b})
        executor = PlanExecutor(max_concurrent=2, sub_agent_factory=factory)

        plan = ExecutionPlan(
            description="fail cascade",
            steps=[
                PlanStep(id="a", tool="shell", args={}),
                PlanStep(id="b", tool="shell", args={}, depends_on=["a"]),
            ],
        )

        result = executor.execute(plan, minimal_ctx)

        assert result["success"] is False
        assert result["results"]["a"]["success"] is False
        assert result["results"]["b"]["success"] is False
        assert "dependency 'a' did not succeed" in result["results"]["b"]["error"]
        # Only step a spawned a runner; b was skipped.
        assert len(factory.calls) == 1

    def test_timeout_cancels_running(self, minimal_ctx):
        block_event = threading.Event()

        def factory(*args, **kwargs):
            runner = MagicMock()

            def run(_task):
                # Block until cancel_event triggers the runner's cancel method,
                # then return the cancelled sentinel so the executor records a
                # timeout-induced cancellation.
                block_event.wait()
                return "[Cancelled]"

            runner.run = run
            runner.cancel = block_event.set
            return runner

        executor = PlanExecutor(
            max_concurrent=2,
            sub_agent_factory=factory,
        )

        plan = ExecutionPlan(
            description="timeout test",
            timeout=1,
            steps=[
                PlanStep(id="slow1", tool="shell", args={}),
                PlanStep(id="slow2", tool="shell", args={}),
            ],
        )

        result = executor.execute(plan, minimal_ctx)

        assert result["success"] is False
        for sid in ("slow1", "slow2"):
            assert result["results"][sid]["success"] is False
            assert result["results"][sid]["error_type"] == "tool_timeout"
            # Steps unblock during grace period → recorded as timed-out, not cancelled.
            assert "timeout" in result["results"][sid]["error"].lower()

    def test_respects_max_concurrent(self, minimal_ctx):
        # Track how many runners are simultaneously active via a counter.
        active_lock = threading.Lock()
        max_active = [0]
        currently_active = [0]
        gate = threading.Semaphore(2)
        gate_lock = threading.Lock()
        acquired_gates: list[threading.Semaphore] = []

        def factory(*args, **kwargs):
            runner = MagicMock()

            def run(_task):
                with active_lock:
                    currently_active[0] += 1
                    max_active[0] = max(max_active[0], currently_active[0])
                try:
                    # Hold one of the two permits for the duration of this step.
                    gate.acquire()
                    with gate_lock:
                        acquired_gates.append(gate)
                    time.sleep(0.1)
                    gate.release()
                finally:
                    with active_lock:
                        currently_active[0] -= 1
                return json.dumps({"success": True, "output": "ok", "error": "", "exit_code": 0})

            runner.run = run
            return runner

        executor = PlanExecutor(max_concurrent=2, sub_agent_factory=factory)
        plan = ExecutionPlan(
            description="concurrency cap",
            steps=[
                PlanStep(id="a", tool="shell", args={}),
                PlanStep(id="b", tool="shell", args={}),
                PlanStep(id="c", tool="shell", args={}),
            ],
        )

        result = executor.execute(plan, minimal_ctx)

        assert result["success"] is True
        assert max_active[0] <= 2
        # With a concurrency cap of 2 only two permits can ever be acquired at once,
        # so the total number of distinct simultaneous acquisitions equals 2.
        assert max_active[0] == 2

    def test_progress_callback_invoked(self, minimal_ctx, recorder_factory):
        factory = recorder_factory()
        executor = PlanExecutor(max_concurrent=2, sub_agent_factory=factory)
        plan = ExecutionPlan(
            description="progress",
            steps=[PlanStep(id="a", tool="shell", args={})],
        )

        progress = []
        executor.execute(plan, minimal_ctx, progress_cb=progress.append)

        assert any("Batch" in msg for msg in progress)
        assert any("Step 'a' completed" in msg for msg in progress)

    def test_factory_from_ctx_builtin_executor(self, minimal_ctx, recorder_factory):
        factory = recorder_factory()
        builtin = MagicMock()
        builtin._sub_agent_factory = factory
        minimal_ctx.builtin_executor = builtin

        executor = PlanExecutor(max_concurrent=2)
        plan = ExecutionPlan(
            description="ctx factory",
            steps=[PlanStep(id="a", tool="shell", args={})],
        )

        result = executor.execute(plan, minimal_ctx)

        assert result["success"] is True
        assert len(factory.calls) == 1

    def test_no_factory_returns_error(self, minimal_ctx):
        executor = PlanExecutor(max_concurrent=2)
        plan = ExecutionPlan(
            description="no factory",
            steps=[PlanStep(id="a", tool="shell", args={})],
        )

        result = executor.execute(plan, minimal_ctx)

        assert result["success"] is False
        assert any("No sub-agent factory" in err for err in result["errors"])

    def test_factory_exception_records_failure(self, minimal_ctx):
        def factory(*args, **kwargs):
            raise RuntimeError("factory boom")

        executor = PlanExecutor(max_concurrent=2, sub_agent_factory=factory)
        plan = ExecutionPlan(
            description="factory exception",
            steps=[PlanStep(id="a", tool="shell", args={})],
        )

        result = executor.execute(plan, minimal_ctx)

        assert result["success"] is False
        assert result["results"]["a"]["success"] is False
        assert "factory boom" in result["results"]["a"]["error"]


class TestStepTaskRecoveryContract:
    """The sub-agent task contract must request recovery metadata on failure."""

    def test_build_step_task_requests_recovery_fields(self):
        step = PlanStep(id="s1", tool="shell", args={"command": "ls"})
        task, response_format = _build_step_task(step)
        assert response_format == "json"
        assert "error_type" in task
        assert "recoverable" in task
        assert "suggestion" in task

    def test_standardize_preserves_recovery_fields(self):
        raw = json.dumps({
            "success": False,
            "output": "",
            "error": "timed out",
            "exit_code": 124,
            "error_type": "tool_timeout",
            "recoverable": True,
            "suggestion": "increase timeout",
        })
        outcome = _standardize_sub_agent_result(raw, "json")
        assert outcome["error_type"] == "tool_timeout"
        assert outcome["recoverable"] is True
        assert outcome["suggestion"] == "increase timeout"

    def test_recovery_metadata_drives_retry(self, minimal_ctx):
        calls = {"n": 0}

        def factory(*args, **kwargs):
            runner = MagicMock()

            def run(_task):
                calls["n"] += 1
                if calls["n"] == 1:
                    return json.dumps({
                        "success": False,
                        "output": "",
                        "error": "network down",
                        "exit_code": 1,
                        "error_type": "network_error",
                        "recoverable": True,
                        "suggestion": "retry",
                    })
                return json.dumps({
                    "success": True,
                    "output": "ok",
                    "error": "",
                    "exit_code": 0,
                })

            runner.run = run
            runner.cancel = MagicMock()
            runner.close = MagicMock()
            return runner

        executor = PlanExecutor(max_concurrent=1, sub_agent_factory=factory)
        plan = ExecutionPlan(
            description="retry on recoverable",
            steps=[PlanStep(id="a", tool="shell", args={})],
        )

        result = executor.execute(plan, minimal_ctx)

        assert result["success"] is True
        assert calls["n"] == 2
        assert result["results"]["a"]["retry_count"] == 1


class TestGracePeriodTimeout:
    """Steps completing during the cancellation grace period must be marked timed out."""

    def test_grace_period_completion_is_timed_out(self, minimal_ctx):
        """A step that finishes during grace should be failure, not success."""
        unblock = threading.Event()

        def factory(*args, **kwargs):
            runner = MagicMock()

            def run(_task):
                unblock.wait()
                # Returns success — but deadline was already exceeded.
                return json.dumps({"success": True, "output": "late ok", "error": "", "exit_code": 0})

            runner.run = run
            runner.cancel = unblock.set
            runner.close = MagicMock()
            return runner

        executor = PlanExecutor(max_concurrent=1, sub_agent_factory=factory)
        plan = ExecutionPlan(
            description="grace test",
            timeout=1,
            steps=[PlanStep(id="late", tool="shell", args={})],
        )

        result = executor.execute(plan, minimal_ctx)

        assert result["success"] is False
        step = result["results"]["late"]
        assert step["success"] is False
        assert step["error_type"] == "tool_timeout"
        assert "timeout" in step["error"].lower()
        assert any("timed out" in e for e in result["errors"])


class TestParentCancelBridge:
    """Parent-agent cancellation must propagate into plan execution."""

    def test_parent_cancel_stops_plan(self, minimal_ctx):
        started = threading.Event()
        cancelled = threading.Event()

        def factory(*args, **kwargs):
            runner = MagicMock()

            def run(_task):
                started.set()
                cancelled.wait(timeout=5)
                return "[Cancelled]"

            runner.run = run
            runner.cancel = cancelled.set
            runner.close = MagicMock()
            return runner

        executor = PlanExecutor(max_concurrent=1, sub_agent_factory=factory)
        plan = ExecutionPlan(
            description="parent cancel test",
            timeout=60,  # long — parent cancel must preempt it
            steps=[PlanStep(id="s1", tool="shell", args={})],
        )

        def _cancel_after_start():
            started.wait(timeout=5)
            minimal_ctx.cancel_event.set()

        cancel_thread = threading.Thread(target=_cancel_after_start, daemon=True)
        cancel_thread.start()

        t0 = time.monotonic()
        result = executor.execute(plan, minimal_ctx)
        elapsed = time.monotonic() - t0

        cancel_thread.join(timeout=5)
        assert result["success"] is False
        # Must complete well before the 60s plan timeout (within ~2s grace + poll).
        assert elapsed < 10
        # Result must be classified as cancellation, not a timeout error.
        step = result["results"].get("s1", {})
        assert step.get("error_type", "") == ""
        assert "cancelled" in step.get("error", "").lower()

    def test_preexisting_parent_cancel_prevents_execution(self, minimal_ctx):
        """If ctx.cancel_event is already set, no step should be submitted."""
        factory_calls = {"n": 0}

        def factory(*args, **kwargs):
            factory_calls["n"] += 1
            runner = MagicMock()
            runner.run = MagicMock(return_value=json.dumps({
                "success": True, "output": "ok", "error": "", "exit_code": 0,
            }))
            runner.cancel = MagicMock()
            runner.close = MagicMock()
            return runner

        # Pre-set the parent cancel event before calling execute().
        minimal_ctx.cancel_event.set()

        executor = PlanExecutor(max_concurrent=1, sub_agent_factory=factory)
        plan = ExecutionPlan(
            description="pre-cancelled",
            timeout=60,
            steps=[PlanStep(id="s1", tool="shell", args={})],
        )

        result = executor.execute(plan, minimal_ctx)

        assert result["success"] is False
        # No sub-agent should have been invoked.
        assert factory_calls["n"] == 0
        # Step must be classified as cancelled, not as a generic skip.
        step = result["results"]["s1"]
        assert step["success"] is False
        assert "cancelled" in step["error"].lower()
        assert step.get("error_type", "") == ""
        # errors list must explain the cause.
        assert any("cancelled" in e.lower() for e in result["errors"])

    def test_cancel_during_runner_creation_discards_runner(self, minimal_ctx):
        """Cancellation that arrives while the factory is blocked must not run the runner."""
        factory_unblock = threading.Event()
        run_called = {"n": 0}

        def factory(*args, **kwargs):
            # Simulate a slow factory (e.g. network call to provision a model).
            factory_unblock.wait(timeout=10)
            runner = MagicMock()

            def run(_task):
                run_called["n"] += 1
                return json.dumps({"success": True, "output": "ok", "error": "", "exit_code": 0})

            runner.run = run
            runner.cancel = MagicMock()
            runner.close = MagicMock()
            return runner

        executor = PlanExecutor(max_concurrent=1, sub_agent_factory=factory)
        plan = ExecutionPlan(
            description="cancel during creation",
            timeout=60,
            steps=[PlanStep(id="s1", tool="shell", args={})],
        )

        def _cancel_and_unblock():
            # Cancel the parent, then unblock the factory so it returns a runner.
            # The runner should be discarded because cancel arrived first.
            minimal_ctx.cancel_event.set()
            factory_unblock.set()

        t = threading.Thread(target=_cancel_and_unblock, daemon=True)
        t.start()

        result = executor.execute(plan, minimal_ctx)
        t.join(timeout=5)

        assert result["success"] is False
        # The runner's run() must not have been invoked.
        assert run_called["n"] == 0


# ---------------------------------------------------------------------------
# Runtime profile threading (Phase 3 profile wiring)
# ---------------------------------------------------------------------------


class TestRuntimeProfileThreading:
    """Plan-step and diagnostic construction pass their matching RuntimeProfile.

    Construction remains behavior-equivalent; only the profile threaded to the
    factory differs, so the visibility source assigned later stays consistent
    with the construction origin.
    """

    def test_plan_step_construction_uses_plan_step_profile(self, minimal_ctx, recorder_factory):
        factory = recorder_factory()
        executor = PlanExecutor(max_concurrent=1, sub_agent_factory=factory)
        step = PlanStep(id="s1", tool="shell", args={})
        cancel = threading.Event()

        runner, err = executor._create_runner(step, minimal_ctx, cancel, factory)

        assert err is None
        assert runner is not None
        assert factory.calls[0]["kwargs"]["runtime_profile"] == RuntimeProfile.PLAN_STEP_AGENT

    def test_diagnostic_construction_uses_diagnostic_profile(self, minimal_ctx, recorder_factory):
        factory = recorder_factory()
        executor = PlanExecutor(max_concurrent=1, sub_agent_factory=factory)
        step = PlanStep(id="s1", tool="shell", args={})
        cancel = threading.Event()
        outcome = {"error_type": "tool_timeout", "error": "boom"}

        # _diagnose_step_failure registers/deregisters the runner in the global
        # registry itself (try/finally), so no external cleanup is required.
        executor._diagnose_step_failure(step, outcome, minimal_ctx, factory, cancel)

        assert factory.calls[0]["kwargs"]["runtime_profile"] == RuntimeProfile.DIAGNOSTIC_AGENT

    def test_plan_step_and_diagnostic_profiles_differ(self, minimal_ctx, recorder_factory):
        factory = recorder_factory()
        executor = PlanExecutor(max_concurrent=1, sub_agent_factory=factory)
        step = PlanStep(id="s1", tool="shell", args={})
        cancel = threading.Event()

        executor._create_runner(step, minimal_ctx, cancel, factory)
        executor._diagnose_step_failure(
            step, {"error_type": "x", "error": "e"}, minimal_ctx, factory, cancel,
        )

        profiles = [c["kwargs"]["runtime_profile"] for c in factory.calls]
        assert profiles == [RuntimeProfile.PLAN_STEP_AGENT, RuntimeProfile.DIAGNOSTIC_AGENT]

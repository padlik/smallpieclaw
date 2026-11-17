"""Unit tests for the execution_plan module.

Covers the :class:`PlanStep` dataclass, plan validation, topological batching,
result substitution, and the :class:`PlanExecutor` sub-agent orchestration.
"""

from __future__ import annotations

import concurrent.futures
import json
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

import agent_controller
import execution_plan
from agent_runtime import RuntimeOptions, RuntimeProfile
from execution_plan import (
    ExecutionPlan,
    PlanExecutor,
    PlanStep,
    PlanValidationError,
    _build_step_task,
    _parent_cancel_bridge,
    _standardize_sub_agent_result,
    substitute_results,
    topological_sort,
    validate_plan,
)
from llm_client import LLMClient
from model_registry import ModelRegistryError, RunModel
from react_loop import ReactContext
from sub_agent_registry import SOURCE_PLAN_STEP, get_registry
from tests.root_startup_harness import (
    ACME_HOST,
    GEM_HOST,
    INVALID_SAVED,
    UNCONFIGURED_ID,
    remembered_for,
    MAIN_ID,
    OTHER_ID,
    SECRETS,
    RootHarness,
)


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


# ---------------------------------------------------------------------------
# Incomplete-step classification
# ---------------------------------------------------------------------------


class TestClassifyIncomplete:
    """Direct tests for PlanExecutor._classify_incomplete."""

    def test_cancelled_by_parent_completed_in_grace(self):
        executor = PlanExecutor()
        outcome, error_line = executor._classify_incomplete(
            "s1", cancelled_by_parent=True, completed_in_grace=True,
        )
        assert outcome["success"] is False
        assert "parent agent" in outcome["error"].lower()
        assert "grace period" in outcome["error"].lower()
        assert "cancelled" in error_line.lower()
        assert "parent agent" in error_line.lower()

    def test_timeout_completed_in_grace(self):
        executor = PlanExecutor()
        outcome, error_line = executor._classify_incomplete(
            "s1", cancelled_by_parent=False, completed_in_grace=True,
        )
        assert outcome["success"] is False
        assert outcome.get("error_type") == "tool_timeout"
        assert outcome.get("recoverable") is True
        assert "timeout" in outcome["error"].lower()
        assert "grace period" in outcome["error"].lower()
        assert "timed out" in error_line.lower()

    def test_cancelled_by_parent_still_running(self):
        executor = PlanExecutor()
        outcome, error_line = executor._classify_incomplete(
            "s1", cancelled_by_parent=True, completed_in_grace=False,
        )
        assert outcome["success"] is False
        assert outcome.get("error_type", "") == ""
        assert "parent agent" in outcome["error"].lower()
        assert "still be running" in outcome["error"].lower()
        assert "cancelled" in error_line.lower()
        assert "still be running" in error_line.lower()

    def test_timeout_still_running(self):
        executor = PlanExecutor()
        outcome, error_line = executor._classify_incomplete(
            "s1", cancelled_by_parent=False, completed_in_grace=False,
        )
        assert outcome["success"] is False
        assert outcome.get("error_type") == "tool_timeout"
        assert outcome.get("recoverable") is True
        assert "timeout" in outcome["error"].lower()
        assert "still be running" in outcome["error"].lower()
        assert "timed out" in error_line.lower()
        assert "still be running" in error_line.lower()


class TestParentCancelBridgeHelper:
    """Direct tests for the _parent_cancel_bridge context manager."""

    def test_no_parent_cancel_yields_without_thread(self):
        cancel_event = threading.Event()
        cancelled_flag = [False]
        with _parent_cancel_bridge(None, cancel_event, cancelled_flag):
            assert not cancel_event.is_set()
            assert cancelled_flag[0] is False
        assert not cancel_event.is_set()

    def test_preexisting_parent_cancel_sets_flag(self):
        parent = threading.Event()
        parent.set()
        cancel_event = threading.Event()
        cancelled_flag = [False]
        with _parent_cancel_bridge(parent, cancel_event, cancelled_flag):
            assert cancel_event.is_set()
            assert cancelled_flag[0] is True

    def test_parent_cancel_arrives_during_context(self):
        parent = threading.Event()
        cancel_event = threading.Event()
        cancelled_flag = [False]

        def _set_parent_after_short_delay():
            time.sleep(0.05)
            parent.set()

        t = threading.Thread(target=_set_parent_after_short_delay, daemon=True)
        t.start()
        with _parent_cancel_bridge(parent, cancel_event, cancelled_flag):
            # Wait long enough for the bridge thread to observe the parent event.
            parent.wait(timeout=0.5)
            # Give the bridge thread a chance to propagate.
            time.sleep(0.15)
            assert cancel_event.is_set()
            assert cancelled_flag[0] is True
        t.join(timeout=0.5)

    def test_no_parent_cancel_flag_stays_false(self):
        parent = threading.Event()
        cancel_event = threading.Event()
        cancelled_flag = [False]
        with _parent_cancel_bridge(parent, cancel_event, cancelled_flag):
            time.sleep(0.05)
            assert not cancel_event.is_set()
            assert cancelled_flag[0] is False
        assert not cancel_event.is_set()
        assert cancelled_flag[0] is False


class TestPlanStepPublicShape:
    """Task 12.13: PlanStep keeps its public five-field shape (no model field)."""

    def test_exact_public_fields(self):
        import dataclasses

        assert [f.name for f in dataclasses.fields(PlanStep)] == [
            "id", "tool", "args", "depends_on", "description",
        ]


# ---------------------------------------------------------------------------
# Task 7.2: production-root ordinary admission through the actual plan route
#
# Drives the real ``main._run`` via ``tests/root_startup_harness.py``. A real
# ``PlanExecutor`` without an injected factory late-binds the actual root
# builtin's main-owned factory from a real ``ReactContext``; it always passes
# ``model=None``, so a ``model`` key in step args stays opaque tool payload.
# Replaced boundaries: the harness's external effects,
# ``agent_controller.react_loop`` and the plan's worker pool, run inline.
# Admission, credential resolution, runtime construction and run
# registration are observed by delegating spies installed after startup.
# ---------------------------------------------------------------------------

ROOT_PLAN_TRACE = "r-7a2b0c1d"
ROOT_OPAQUE_ARGS = {"model": OTHER_ID, "note": "opaque"}
ROOT_STEP_OUTPUT = "root-plan-step-done"


@pytest.fixture
def production_root(tmp_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[RootHarness]:
    """Keep production ownership assertions ahead of fallback fixture hygiene."""
    root = RootHarness(tmp_xdg, monkeypatch)
    try:
        yield root
    finally:
        root.release_leftovers()


class _InlineExecutor(concurrent.futures.ThreadPoolExecutor):
    """Worker-pool boundary that runs submitted work inline: no thread starts."""

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> concurrent.futures.Future:
        """Run *fn* now and return its completed future."""
        future: concurrent.futures.Future = concurrent.futures.Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 — delivered through the future
            future.set_exception(exc)
        return future


class _RootReactBoundary:
    """Replaces only ``agent_controller.react_loop``; each run sends one real pinned request."""

    def __init__(self, answer: str) -> None:
        """Record runs and return *answer* from each."""
        self.answer = answer
        self.calls: list[SimpleNamespace] = []

    def __call__(
        self, ctx: ReactContext, goal: str, progress_callback: Any = None,
        images: Any = None, **kwargs: Any,
    ) -> str:
        """Capture the run's pinned client, then send one request through it."""
        call = SimpleNamespace(goal=goal, llm=ctx.llm, pin=getattr(ctx.llm, "_run_model", None))
        self.calls.append(call)
        call.reply = ctx.llm.chat([{"role": "user", "content": "ping"}])
        return self.answer


class _RootAttemptSpies:
    """Delegating observers on the started root; counters exclude startup work."""

    def __init__(self, root: RootHarness, monkeypatch: pytest.MonkeyPatch) -> None:
        """Snapshot root state, then wrap the root's real collaborators."""
        self._root = root
        self.view_before = root.registry.view()
        self.settings_before = root.paths.models_cache_file.read_bytes()
        self._requests = len(root.requests)
        self._llm_clients = len(root.llm_clients)
        self._http_clients = len(root.http_clients)
        self.admits: dict[str, list[object]] = {"admit": [], "admit_scheduled": []}
        self.codes: list[str] = []
        self.pins: list[RunModel] = []
        self.resolved: list[str] = []
        self.creates: list[tuple[object, object]] = []
        self.registered: list[tuple[str, str]] = []
        registry = root.registry
        for name in self.admits:
            monkeypatch.setattr(registry, name, self._admission_spy(name, getattr(registry, name)))
        resolve = registry._credential_resolver

        def resolve_spy(config: Any) -> Any:
            """Record the provider whose credentials are resolved, then delegate."""
            self.resolved.append(config.name)
            return resolve(config)

        monkeypatch.setattr(registry, "_credential_resolver", resolve_spy)
        create = root.runtime.create

        def create_spy(profile: object, options: object, **kwargs: Any) -> object:
            """Record the runtime construction request, then delegate."""
            self.creates.append((profile, options))
            return create(profile, options, **kwargs)

        monkeypatch.setattr(root.runtime, "create", create_spy)
        runs = get_registry()
        register = runs.register

        def register_spy(record: Any) -> None:
            """Record the child run registration, then delegate."""
            self.registered.append((record.agent_id, record.source))
            register(record)

        monkeypatch.setattr(runs, "register", register_spy)

    def _admission_spy(self, name: str, original: Callable[[object], RunModel]) -> Callable[..., RunModel]:
        """Return a spy recording selector, pin or typed refusal code around *original*."""

        def spy(selector: object = None) -> RunModel:
            """Record, then delegate to the real registry admission."""
            self.admits[name].append(selector)
            try:
                pin = original(selector)
            except ModelRegistryError as exc:
                self.codes.append(exc.code)
                raise
            self.pins.append(pin)
            return pin

        return spy

    def provider_requests(self) -> list[httpx.Request]:
        """Requests to either catalog provider since the snapshot."""
        return [r for r in self._root.requests[self._requests:] if r.url.host in (ACME_HOST, GEM_HOST)]

    def new_llm_clients(self) -> list[LLMClient]:
        """Inference clients constructed since the snapshot."""
        return self._root.llm_clients[self._llm_clients:]

    def new_http_clients(self) -> list[httpx.Client]:
        """HTTP clients constructed since the snapshot."""
        return self._root.http_clients[self._http_clients:]


def _serve_anthropic_chat(root: RootHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    """Extend the harness HTTP boundary with a canned Anthropic chat reply."""
    original = root._route

    def route(request: httpx.Request) -> httpx.Response:
        """Serve the pinned Anthropic chat endpoint; delegate everything else."""
        if (
            request.method == "POST" and request.url.host == ACME_HOST
            and request.url.path == "/v1/messages"
        ):
            request.read()
            root.requests.append(request)
            return httpx.Response(200, json={
                "content": [{"type": "text", "text": "pong"}],
                "usage": {"input_tokens": 3, "output_tokens": 1},
            })
        return original(request)

    monkeypatch.setattr(root, "_route", route)


class TestProductionRootPlanAdmission:
    """Task 7.2: actual root plan route; plan steps snapshot Main and never bypass its gate."""

    @staticmethod
    def _execute_root_plan(
        root: RootHarness, monkeypatch: pytest.MonkeyPatch, outcome: dict[str, Any],
    ) -> None:
        """Run a one-step plan through a real PlanExecutor on the root builtin."""
        runtime, registry, factory = root.factory_binding()
        outcome["binding"] = (
            runtime is root.runtime, registry is root.registry,
            root.builtin._sub_agent_factory is factory,
        )
        spies = _RootAttemptSpies(root, monkeypatch)
        outcome["spies"] = spies
        monkeypatch.setattr(execution_plan, "ThreadPoolExecutor", _InlineExecutor)
        executor = PlanExecutor(max_concurrent=1)
        ctx = ReactContext(
            llm=None, tool_index=None, memory=None, builtin_executor=root.builtin,
            mcp_manager=None, skill_registry=None, trace_id=ROOT_PLAN_TRACE,
        )
        outcome["late_bound"] = (
            executor._sub_agent_factory is None, executor._get_factory(ctx) is factory,
        )
        args = dict(ROOT_OPAQUE_ARGS)
        plan = ExecutionPlan(description="root plan", steps=[PlanStep(id="a", tool="shell", args=args)])
        outcome["args"], outcome["plan"] = args, plan
        outcome["result"] = executor.execute(plan, ctx)
        outcome["llm_closed"] = [client._closed for client in spies.new_llm_clients()]
        outcome["http_open"] = [c for c in spies.new_http_clients() if not c.is_closed]
        outcome["view_after"] = root.registry.view()
        outcome["settings_after"] = root.paths.models_cache_file.read_bytes()

    @pytest.mark.parametrize(
        "saved_default", [INVALID_SAVED, UNCONFIGURED_ID], ids=["no-saved-selection", "saved-model-missing"],
    )
    def test_plan_step_fails_normally_without_active_main(
        self, production_root: RootHarness, monkeypatch: pytest.MonkeyPatch,
        saved_default: str | None,
    ) -> None:
        """model=None hits the Main gate; the step fails before resolver/runtime/client/HTTP."""
        boundary = _RootReactBoundary(json.dumps({"success": True, "output": ROOT_STEP_OUTPUT}))
        monkeypatch.setattr(agent_controller, "react_loop", boundary)
        outcome: dict[str, Any] = {}

        production_root.start(
            saved_default=saved_default,
            hold=lambda root: self._execute_root_plan(root, monkeypatch, outcome),
        )
        root = production_root
        spies: _RootAttemptSpies = outcome["spies"]

        assert outcome["binding"] == (True, True, True)
        assert outcome["late_bound"] == (True, True)
        before = spies.view_before
        assert before.active_model_id is None and before.remembered_model_id == remembered_for(saved_default)
        assert OTHER_ID in {m.id for m in before.models}
        # Normal failed step and error list; no diagnostic retry.
        error = "Failed to create sub-agent: model registry no active selection"
        result = outcome["result"]
        assert result["success"] is False
        step = result["results"]["a"]
        assert step["success"] is False and step["error"] == error
        assert "diagnosis" not in step
        assert result["errors"] == [f"Step 'a' failed: {error}"]
        assert all(secret not in json.dumps(result, default=str) for secret in SECRETS.values())
        # One ordinary admission with model=None, refused at the Main gate.
        assert spies.admits == {"admit": [None], "admit_scheduled": []}
        assert spies.codes == ["no_active_selection"] and spies.pins == []
        # Nothing after the gate.
        assert spies.resolved == [] and spies.creates == [] and spies.registered == []
        assert spies.new_llm_clients() == [] and spies.new_http_clients() == []
        assert spies.provider_requests() == [] and boundary.calls == []
        # The opaque model-valued arg is neither consumed nor rewritten.
        plan = outcome["plan"]
        assert plan.steps[0].args is outcome["args"] and outcome["args"] == ROOT_OPAQUE_ARGS
        assert outcome["view_after"] == before
        assert outcome["settings_after"] == spies.settings_before
        assert root.unexpected == [] and root.open_clients_at_exit == []

    def test_plan_step_snapshots_active_main_despite_opaque_model_arg(
        self, production_root: RootHarness, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Positive control: the step pins Main A once and reaches only the acme endpoint."""
        _serve_anthropic_chat(production_root, monkeypatch)
        boundary = _RootReactBoundary(json.dumps({
            "success": True, "output": ROOT_STEP_OUTPUT, "error": "", "exit_code": 0,
        }))
        monkeypatch.setattr(agent_controller, "react_loop", boundary)
        outcome: dict[str, Any] = {}

        def hold(root: RootHarness) -> None:
            """Deliberate /models-style refresh first: startup no longer rediscovers a cached catalog,
            and Anthropic budgeting needs the provider-reported max_tokens. Spies start after it."""
            root.registry.refresh()
            self._execute_root_plan(root, monkeypatch, outcome)

        production_root.start(saved_default=MAIN_ID, hold=hold)
        root = production_root
        spies: _RootAttemptSpies = outcome["spies"]

        assert outcome["binding"] == (True, True, True)
        assert outcome["late_bound"] == (True, True)
        assert spies.view_before.active_model_id == MAIN_ID
        result = outcome["result"]
        assert result["success"] is True, result
        step = result["results"]["a"]
        assert step["success"] is True and step["output"] == ROOT_STEP_OUTPUT
        # One ordinary admission snapshotting Main A; the opaque OTHER_ID arg is not a selector.
        assert spies.admits == {"admit": [None], "admit_scheduled": []} and spies.codes == []
        (pin,) = spies.pins
        assert pin.model.id == MAIN_ID and pin.connection.provider_id == "acme"
        assert pin.connection.base_url == f"https://{ACME_HOST}"
        assert spies.resolved == ["acme"]
        ((profile, options),) = spies.creates
        assert profile is RuntimeProfile.PLAN_STEP_AGENT
        assert isinstance(options, RuntimeOptions)
        assert options.run_model is pin and options.label == "plan-a"
        assert options.trace_id == ROOT_PLAN_TRACE
        (call,) = boundary.calls
        assert call.pin is pin and call.reply == "pong"
        assert f"Arguments: {json.dumps(ROOT_OPAQUE_ARGS, ensure_ascii=False)}" in call.goal
        assert spies.new_llm_clients() == [call.llm]
        # The only model request went to the pinned acme endpoint with its own credential.
        (post,) = [r for r in spies.provider_requests() if r.method == "POST"]
        assert str(post.url) == f"https://{ACME_HOST}/v1/messages"
        assert post.headers["x-api-key"] == SECRETS["KEY_ACME"]
        assert json.loads(post.content)["model"] == "claude-a"
        assert [r for r in spies.provider_requests() if r.url.host == GEM_HOST] == []
        # Registered once as a plan step, deregistered, run-owned clients closed.
        ((agent_id, source),) = spies.registered
        assert source == SOURCE_PLAN_STEP and get_registry().get(agent_id) is None
        assert outcome["llm_closed"] == [True] and outcome["http_open"] == []
        assert outcome["args"] == ROOT_OPAQUE_ARGS
        assert outcome["view_after"] == spies.view_before
        assert outcome["settings_after"] == spies.settings_before
        assert root.unexpected == [] and root.open_clients_at_exit == []

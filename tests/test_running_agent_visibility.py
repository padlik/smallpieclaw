"""Tests for unified running-agent visibility and capacity semantics.

Covers OpenSpec change ``unify-running-agent-visibility``:

- registry source categories + capacity model (on-demand/scheduled counted;
  plan-step/diagnostic visible but not counted)
- ``register_run``/``deregister_run`` shared wiring helper
- plan-step and diagnostic runners become visible while running and are
  cleaned up (including on cancel/timeout and diagnostic build/run failure)
- ``/agents`` source labels + managed-cancel capacity scope + explicit cancel
- ``/status`` active-agent count includes all visible registry records
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution_plan import (  # noqa: E402
    ExecutionPlan,
    PlanExecutor,
    PlanStep,
)
from react_loop import ReactContext  # noqa: E402
from sub_agent_registry import (  # noqa: E402
    CAPACITY_COUNTED_SOURCES,
    SOURCE_DIAGNOSTIC,
    SOURCE_ON_DEMAND,
    SOURCE_PLAN_STEP,
    SOURCE_SCHEDULED,
    SubAgentRecord,
    SubAgentRegistry,
    deregister_run,
    get_registry,
    register_run,
)
from telegram_commands import cmd_agents, cmd_status  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------

class FakeRunner:
    """Minimal SubAgentRunner stand-in compatible with register_run."""

    _counter = 0

    def __init__(self, *, label: str = "plan-x", model_id: str = "test-model",
                 max_iterations: int = 5, run_impl=None):
        FakeRunner._counter += 1
        self.agent_id = f"sa-{FakeRunner._counter:03d}"
        self.label = label
        self._model_id = model_id
        self._cancel_event = threading.Event()
        self._llm = MagicMock()
        self._agent = SimpleNamespace(max_iterations=max_iterations, _on_step=None)
        self.closed = False
        self._run_impl = run_impl

    def run(self, task: str) -> str:
        if self._run_impl is not None:
            return self._run_impl(self, task)
        return '{"success": true, "output": "ok", "error": "", "exit_code": 0}'

    def cancel(self) -> None:
        self._cancel_event.set()

    def close(self) -> None:
        self.closed = True


def _make_record(source: str, agent_id: str, label: str = "") -> SubAgentRecord:
    return SubAgentRecord(
        agent_id=agent_id,
        label=label or agent_id,
        model="test-model",
        task_preview="task preview",
        started_at=time.time(),
        source=source,
    )


def _minimal_ctx() -> ReactContext:
    return ReactContext(
        llm=MagicMock(),
        tool_index=MagicMock(),
        executor=MagicMock(),
        creator=MagicMock(),
        memory=MagicMock(),
        builtin_executor=None,
        mcp_manager=None,
        skill_registry=None,
    )


@pytest.fixture(autouse=True)
def _clean_singleton_registry():
    """Isolate tests from the module-level singleton registry."""
    yield
    reg = get_registry()
    for rec in reg.list_active():
        reg.unregister(rec.agent_id)


# ---------------------------------------------------------------------------
# Registry source + capacity model
# ---------------------------------------------------------------------------

class TestRegistryCapacityModel:
    def test_capacity_counted_sources_are_on_demand_and_scheduled(self):
        assert CAPACITY_COUNTED_SOURCES == frozenset(
            {SOURCE_ON_DEMAND, SOURCE_SCHEDULED}
        )

    def test_count_managed_counts_only_capacity_sources(self):
        reg = SubAgentRegistry()
        reg.register(_make_record(SOURCE_ON_DEMAND, "sa-1"))
        reg.register(_make_record(SOURCE_SCHEDULED, "sa-2", label="nightly"))
        reg.register(_make_record(SOURCE_PLAN_STEP, "sa-3"))
        reg.register(_make_record(SOURCE_DIAGNOSTIC, "sa-4"))

        # All four are visible; only two count against the global cap.
        assert len(reg.list_active()) == 4
        assert reg.count() == 4
        assert reg.count_managed() == 2

    def test_cancel_all_managed_targets_only_capacity_sources(self):
        reg = SubAgentRegistry()
        on_demand = _make_record(SOURCE_ON_DEMAND, "sa-1")
        scheduled = _make_record(SOURCE_SCHEDULED, "sa-2", label="nightly")
        plan_step = _make_record(SOURCE_PLAN_STEP, "sa-3")
        diagnostic = _make_record(SOURCE_DIAGNOSTIC, "sa-4")
        for rec in (on_demand, scheduled, plan_step, diagnostic):
            reg.register(rec)

        cancelled = reg.cancel_all_managed()

        assert cancelled == 2
        assert on_demand.is_cancelled is True
        assert scheduled.is_cancelled is True
        # Visible-but-not-counted sources are untouched by managed cancel.
        assert plan_step.is_cancelled is False
        assert diagnostic.is_cancelled is False

    def test_explicit_cancel_works_for_all_visible_sources(self):
        reg = SubAgentRegistry()
        plan_step = _make_record(SOURCE_PLAN_STEP, "sa-plan", label="plan-a")
        diagnostic = _make_record(SOURCE_DIAGNOSTIC, "sa-diag", label="diagnose-a")
        reg.register(plan_step)
        reg.register(diagnostic)

        # Cancel plan-step by id, diagnostic by label — both must succeed.
        assert reg.cancel("sa-plan") is True
        assert reg.cancel("diagnose-a") is True
        assert plan_step.is_cancelled is True
        assert diagnostic.is_cancelled is True


class TestRegisterRunHelper:
    def test_register_run_wires_and_registers(self):
        runner = FakeRunner(label="plan-a", model_id="m1")

        record = register_run(
            runner,
            source=SOURCE_PLAN_STEP,
            label=runner.label,
            task_preview="a" * 200,
            result_type="json",
        )

        assert get_registry().get(runner.agent_id) is record
        assert record.source == SOURCE_PLAN_STEP
        assert record.label == "plan-a"
        assert record.model == "m1"
        assert record.result_type == "json"
        assert len(record.task_preview) == 80  # truncated
        # Shared cancel event + LLM client so /agents cancel can interrupt.
        assert record._cancel_event is runner._cancel_event
        assert record._llm_client is runner._llm
        # on-step wiring updates the record's iteration.
        runner._agent._on_step(3)
        assert get_registry().get(runner.agent_id).iteration == 3

        deregister_run(runner.agent_id)
        assert get_registry().get(runner.agent_id) is None


# ---------------------------------------------------------------------------
# Plan-step + diagnostic visibility and cleanup
# ---------------------------------------------------------------------------

class TestPlanStepVisibility:
    def test_plan_step_visible_while_running_and_cleaned_up(self):
        snapshots: list[dict] = []

        def run_impl(runner, _task):
            active = get_registry().list_active()
            rec = get_registry().get(runner.agent_id)
            snapshots.append({
                "count": len(active),
                "source": rec.source if rec else None,
                "managed": get_registry().count_managed(),
            })
            return '{"success": true, "output": "ok", "error": "", "exit_code": 0}'

        def factory(**kwargs):
            return FakeRunner(label=kwargs.get("label", "plan-x"), run_impl=run_impl)

        executor = PlanExecutor(max_concurrent=1, sub_agent_factory=factory)
        plan = ExecutionPlan(
            description="visible step",
            steps=[PlanStep(id="a", tool="shell", args={})],
        )

        result = executor.execute(plan, _minimal_ctx())

        assert result["success"] is True
        # While running the step was visible with source plan-step and did NOT
        # count against the global capacity guard.
        assert snapshots and snapshots[0]["source"] == SOURCE_PLAN_STEP
        assert snapshots[0]["count"] == 1
        assert snapshots[0]["managed"] == 0
        # No permanent record remains after the step thread unwinds.
        assert get_registry().list_active() == []

    def test_plan_cancel_timeout_leaves_no_stale_record(self):
        block = threading.Event()

        def run_impl(runner, _task):
            # Block until cancelled (via runner.cancel -> event) then return
            # the cancelled sentinel like a real runner would.
            runner._cancel_event.wait(timeout=5.0)
            block.set()
            return "[Cancelled]"

        def factory(**kwargs):
            return FakeRunner(label=kwargs.get("label", "plan-x"), run_impl=run_impl)

        executor = PlanExecutor(max_concurrent=2, sub_agent_factory=factory)
        plan = ExecutionPlan(
            description="timeout",
            timeout=1,
            steps=[
                PlanStep(id="slow1", tool="shell", args={}),
                PlanStep(id="slow2", tool="shell", args={}),
            ],
        )

        result = executor.execute(plan, _minimal_ctx())

        assert result["success"] is False
        # Give any lingering step threads a moment to run their finally blocks.
        deadline = time.time() + 5.0
        while get_registry().list_active() and time.time() < deadline:
            time.sleep(0.05)
        assert get_registry().list_active() == []


class TestDiagnosticVisibility:
    def _diag_step(self):
        return PlanStep(id="a", tool="shell", args={})

    def _outcome(self):
        return {"error_type": "tool_timeout", "error": "boom"}

    def test_diagnostic_visible_while_running_and_cleaned_up(self):
        snapshots: list[dict] = []

        def run_impl(runner, _task):
            rec = get_registry().get(runner.agent_id)
            snapshots.append({
                "source": rec.source if rec else None,
                "managed": get_registry().count_managed(),
            })
            return "diagnosis text"

        def factory(**kwargs):
            return FakeRunner(label=kwargs.get("label", "diagnose-a"), run_impl=run_impl)

        executor = PlanExecutor(max_concurrent=1)
        cancel_event = threading.Event()
        text = executor._diagnose_step_failure(
            self._diag_step(), self._outcome(), _minimal_ctx(), factory, cancel_event,
        )

        assert text == "diagnosis text"
        assert snapshots and snapshots[0]["source"] == SOURCE_DIAGNOSTIC
        # Diagnostic runs do not count against the global capacity guard.
        assert snapshots[0]["managed"] == 0
        assert get_registry().list_active() == []

    def test_diagnostic_run_failure_leaves_no_stale_record(self):
        def run_impl(_runner, _task):
            raise RuntimeError("diagnostic blew up")

        def factory(**kwargs):
            return FakeRunner(label=kwargs.get("label", "diagnose-a"), run_impl=run_impl)

        executor = PlanExecutor(max_concurrent=1)
        text = executor._diagnose_step_failure(
            self._diag_step(), self._outcome(), _minimal_ctx(), factory, threading.Event(),
        )

        assert "unavailable" in text.lower()
        assert get_registry().list_active() == []

    def test_diagnostic_build_failure_registers_nothing(self):
        def factory(**_kwargs):
            raise RuntimeError("cannot build diagnostic runner")

        executor = PlanExecutor(max_concurrent=1)
        text = executor._diagnose_step_failure(
            self._diag_step(), self._outcome(), _minimal_ctx(), factory, threading.Event(),
        )

        assert "unavailable" in text.lower()
        assert get_registry().list_active() == []


# ---------------------------------------------------------------------------
# Telegram operator surface
# ---------------------------------------------------------------------------

def _make_iface():
    from telegram_interface import TelegramInterface

    config: dict = {
        "telegram": {
            "bot_token": "fake:token",
            "security_mode": "allowlist",
            "allowed_user_ids": [42],
        }
    }
    iface = TelegramInterface.__new__(TelegramInterface)
    iface._config = config
    iface.token = "fake:token"
    iface.security_mode = "allowlist"
    iface.allowed_ids = {42}
    iface.agent = None
    iface.scheduler = None
    iface.tool_registry = None
    iface.llm_client = None
    iface.skill_registry = None
    iface._usage_registry = None
    iface.mcp_manager = None
    iface._start_time = time.time()
    return iface


def _make_command_update(args=None):
    captured: dict = {"texts": []}

    async def _reply(text, **_kwargs):
        captured["texts"].append(text)
        return MagicMock()

    mock_message = MagicMock()
    mock_message.reply_text = AsyncMock(side_effect=_reply)
    mock_user = MagicMock()
    mock_user.id = 42
    update = MagicMock()
    update.effective_user = mock_user
    update.effective_message = mock_message
    ctx = MagicMock()
    ctx.args = args or []
    return update, ctx, captured


class TestAgentsCommandSurface:
    def _register_all_sources(self):
        reg = get_registry()
        reg.register(_make_record(SOURCE_ON_DEMAND, "sa-od"))
        reg.register(_make_record(SOURCE_SCHEDULED, "sa-sch", label="nightly"))
        reg.register(_make_record(SOURCE_PLAN_STEP, "sa-plan", label="plan-a"))
        reg.register(_make_record(SOURCE_DIAGNOSTIC, "sa-diag", label="diagnose-a"))

    def test_agents_list_shows_source_labels(self):
        self._register_all_sources()
        iface = _make_iface()
        update, ctx, captured = _make_command_update()

        asyncio.run(cmd_agents(iface, update, ctx))

        text = "\n".join(captured["texts"])
        for source in (SOURCE_ON_DEMAND, SOURCE_SCHEDULED,
                       SOURCE_PLAN_STEP, SOURCE_DIAGNOSTIC):
            assert f"[{source}]" in text

    def test_agents_help_describes_capacity_scope(self):
        # Empty list path mentions managed cancellation scope.
        iface = _make_iface()
        update, ctx, captured = _make_command_update()

        asyncio.run(cmd_agents(iface, update, ctx))

        text = "\n".join(captured["texts"])
        assert "capacity-counted" in text
        assert "on-demand + scheduled" in text

    def test_cancel_managed_only_capacity_counted(self):
        self._register_all_sources()
        reg = get_registry()
        iface = _make_iface()
        update, ctx, captured = _make_command_update(args=["cancel", "managed"])

        asyncio.run(cmd_agents(iface, update, ctx))

        assert reg.get("sa-od").is_cancelled is True
        assert reg.get("sa-sch").is_cancelled is True
        assert reg.get("sa-plan").is_cancelled is False
        assert reg.get("sa-diag").is_cancelled is False

    def test_explicit_cancel_works_for_plan_step_and_diagnostic(self):
        self._register_all_sources()
        reg = get_registry()
        iface = _make_iface()

        update, ctx, _ = _make_command_update(args=["cancel", "sa-plan"])
        asyncio.run(cmd_agents(iface, update, ctx))
        update2, ctx2, _ = _make_command_update(args=["cancel", "diagnose-a"])
        asyncio.run(cmd_agents(iface, update2, ctx2))

        assert reg.get("sa-plan").is_cancelled is True
        assert reg.get("sa-diag").is_cancelled is True


class TestStatusCount:
    def test_status_count_includes_all_visible_records(self):
        reg = get_registry()
        reg.register(_make_record(SOURCE_ON_DEMAND, "sa-od"))
        reg.register(_make_record(SOURCE_SCHEDULED, "sa-sch", label="nightly"))
        reg.register(_make_record(SOURCE_PLAN_STEP, "sa-plan", label="plan-a"))
        reg.register(_make_record(SOURCE_DIAGNOSTIC, "sa-diag", label="diagnose-a"))

        iface = _make_iface()
        update, ctx, captured = _make_command_update()

        asyncio.run(cmd_status(iface, update, ctx))

        text = "\n".join(captured["texts"])
        # All four visible records are counted, not just capacity-counted ones.
        assert "🤖 Sub-agents: 4 running" in text

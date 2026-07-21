"""Tests for /stop cascading cancellation to sub-agents."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sub_agent_registry import SOURCE_ON_DEMAND, SOURCE_SCHEDULED, SOURCE_PLAN_STEP, SubAgentRecord, SubAgentRegistry
import telegram_commands as telegram_commands_mod
from telegram_commands import cmd_stop

def _make_record(agent_id: str, source: str):
    rec = SubAgentRecord(
        agent_id=agent_id,
        label="test",
        model="test-model",
        task_preview="test task",
        started_at=time.time(),
        source=source,
    )
    rec._llm_client = MagicMock()
    return rec


class _FakeAgent:
    def __init__(self):
        self._cancel_event = MagicMock()

    def cancel(self):
        self._cancel_event.set()


class _FakeIface:
    def __init__(self, agent=None, registry=None):
        self.agent = agent
        self._registry = registry

    def _is_authorized(self, uid: int) -> bool:
        return True


class _FakeUpdate:
    def __init__(self):
        self.effective_user = type("_U", (), {"id": 1})()
        self.effective_message = AsyncMock()


@pytest.fixture
def local_registry():
    return SubAgentRegistry()


@pytest.mark.asyncio
async def test_stop_cancels_on_demand_sub_agents(local_registry):
    a = _make_record("sa-a", SOURCE_ON_DEMAND)
    b = _make_record("sa-b", SOURCE_ON_DEMAND)
    local_registry.register(a)
    local_registry.register(b)

    agent = _FakeAgent()
    iface = _FakeIface(agent=agent, registry=local_registry)

    update = _FakeUpdate()
    with patch.object(telegram_commands_mod, "_get_agent_registry", return_value=local_registry):
        await cmd_stop(iface, update, MagicMock())

    assert agent._cancel_event.is_set()
    assert a._cancel_event.is_set()
    assert b._cancel_event.is_set()
    reply = update.effective_message.reply_text
    assert "main agent and all sub-agents cancelling" in reply.call_args[0][0]


@pytest.mark.asyncio
async def test_stop_cancels_scheduled_sub_agents(local_registry):
    s = _make_record("sa-sched", SOURCE_SCHEDULED)
    local_registry.register(s)

    agent = _FakeAgent()
    iface = _FakeIface(agent=agent, registry=local_registry)
    update = _FakeUpdate()

    with patch.object(telegram_commands_mod, "_get_agent_registry", return_value=local_registry):
        await cmd_stop(iface, update, MagicMock())

    assert s._cancel_event.is_set()


@pytest.mark.asyncio
async def test_stop_does_not_directly_cancel_plan_step(local_registry):
    """Plan-step agents are cancelled via the PlanExecutor bridge, not cmd_stop."""
    p = _make_record("sa-plan", SOURCE_PLAN_STEP)
    local_registry.register(p)

    agent = _FakeAgent()
    iface = _FakeIface(agent=agent, registry=local_registry)
    update = _FakeUpdate()

    with patch.object(telegram_commands_mod, "_get_agent_registry", return_value=local_registry):
        await cmd_stop(iface, update, MagicMock())

    # cancel_all_managed targets only SOURCE_ON_DEMAND/SOURCE_SCHEDULED.
    assert not p._cancel_event.is_set()

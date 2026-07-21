"""Tests for the cancel_agent built-in tool."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from builtin_tools import agents as agents_mod

import pytest

from builtin_executor import BuiltinExecutor
from sub_agent_registry import (
    SOURCE_DIAGNOSTIC,
    SOURCE_ON_DEMAND,
    SOURCE_PLAN_STEP,
    SOURCE_SCHEDULED,
    SubAgentRecord,
    SubAgentRegistry,
)


def _make_record(agent_id: str, source: str = SOURCE_ON_DEMAND, status: str = "running"):
    rec = SubAgentRecord(
        agent_id=agent_id,
        label="test",
        model="test-model",
        task_preview="test task",
        started_at=time.time(),
        source=source,
        status=status,
    )
    rec._llm_client = MagicMock()
    return rec


@pytest.fixture
def local_registry():
    return SubAgentRegistry()


@pytest.fixture
def executor(tmp_path):
    return BuiltinExecutor(data_dir=str(tmp_path))


class TestCancelAgent:
    def test_cancel_specific_agent(self, executor, local_registry):
        a = _make_record("sa-a")
        local_registry.register(a)

        with patch.object(agents_mod, "_get_agent_registry", return_value=local_registry):
            result = executor._agents._exec_cancel_agent({"agent_id": "sa-a"})

        assert result["success"] is True
        assert "sa-a" in result["output"]
        assert a._cancel_event.is_set()

    def test_cancel_all_managed_cancels_only_on_demand(self, executor, local_registry):
        a = _make_record("sa-a", source=SOURCE_ON_DEMAND)
        b = _make_record("sa-b", source=SOURCE_ON_DEMAND)
        s = _make_record("sa-sched", source=SOURCE_SCHEDULED)
        p = _make_record("sa-plan", source=SOURCE_PLAN_STEP)
        d = _make_record("sa-diag", source=SOURCE_DIAGNOSTIC)
        for r in (a, b, s, p, d):
            local_registry.register(r)

        with patch.object(agents_mod, "_get_agent_registry", return_value=local_registry):
            result = executor._agents._exec_cancel_agent({"agent_id": "managed"})

        assert result["success"] is True
        assert result["output"] == "Cancelled 2 managed sub-agent(s)."
        assert a._cancel_event.is_set()
        assert b._cancel_event.is_set()
        assert not s._cancel_event.is_set()
        assert not p._cancel_event.is_set()
        assert not d._cancel_event.is_set()

    def test_not_confirmation_gated(self, executor, local_registry):
        a = _make_record("sa-a")
        local_registry.register(a)
        confirm_prompt = MagicMock()
        executor._subagent_confirm_prompt_fn = confirm_prompt

        with patch.object(agents_mod, "_get_agent_registry", return_value=local_registry):
            executor._agents._exec_cancel_agent({"agent_id": "sa-a"})

        assert confirm_prompt.called is False

    def test_unknown_agent_id_handled(self, executor, local_registry):
        with patch.object(agents_mod, "_get_agent_registry", return_value=local_registry):
            result = executor._agents._exec_cancel_agent({"agent_id": "sa-missing"})

        assert result["success"] is False
        assert "sa-missing" in result["error"]

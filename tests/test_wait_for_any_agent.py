"""Tests for the wait_for_any_agent built-in tool."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from sub_agent_registry import SOURCE_ON_DEMAND, SubAgentRecord, SubAgentRegistry
from builtin_tools import agents as agents_mod


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
    return rec


@pytest.fixture
def local_registry():
    return SubAgentRegistry()


@pytest.fixture
def executor(make_builtin_executor, tmp_path):
    return make_builtin_executor(data_dir=str(tmp_path))


class TestWaitForAnyAgent:
    def test_first_completed_returned(self, executor, local_registry):
        a = _make_record("sa-a")
        b = _make_record("sa-b")
        c = _make_record("sa-c")
        for r in (a, b, c):
            local_registry.register(r)

        # b finishes first
        b.status = "done"
        b.result = "B-done"
        b._result_event.set()

        with patch.object(agents_mod, "_get_agent_registry", return_value=local_registry):
            result = executor._agents._exec_wait_for_any_agent(
                {"agent_ids": ["sa-a", "sa-b", "sa-c"], "timeout": 5}
            )
        assert result["status"] == "done"
        assert result["agent_id"] == "sa-b"
        assert result["result"] == "B-done"

    def test_already_finished_returns_immediately(self, executor, local_registry):
        a = _make_record("sa-a", status="done")
        a.result = "A-done"
        a._result_event.set()
        b = _make_record("sa-b")
        local_registry.register(a)
        local_registry.register(b)

        with patch.object(agents_mod, "_get_agent_registry", return_value=local_registry):
            result = executor._agents._exec_wait_for_any_agent(
                {"agent_ids": ["sa-a", "sa-b"], "timeout": 5}
            )
        assert result["status"] == "done"
        assert result["agent_id"] == "sa-a"

    def test_failed_or_cancelled_returned_as_completed(self, executor, local_registry):
        a = _make_record("sa-a", status="failed")
        a.result = "A-crashed"
        a._result_event.set()
        b = _make_record("sa-b")
        local_registry.register(a)
        local_registry.register(b)

        with patch.object(agents_mod, "_get_agent_registry", return_value=local_registry):
            result = executor._agents._exec_wait_for_any_agent(
                {"agent_ids": ["sa-a", "sa-b"], "timeout": 5}
            )
        assert result["status"] == "failed"
        assert result["agent_id"] == "sa-a"
        assert result["error"] == "A-crashed"
        assert result["success"] is False

    def test_timeout_returns_no_result(self, executor, local_registry):
        a = _make_record("sa-a")
        b = _make_record("sa-b")
        local_registry.register(a)
        local_registry.register(b)

        with patch.object(agents_mod, "_get_agent_registry", return_value=local_registry):
            result = executor._agents._exec_wait_for_any_agent(
                {"agent_ids": ["sa-a", "sa-b"], "timeout": 5}
            )
        assert result["status"] == "timeout"
        assert result["success"] is False
        assert "agent_ids" in result

    def test_unknown_agent_id_rejected(self, executor, local_registry):
        a = _make_record("sa-a")
        local_registry.register(a)

        with patch.object(agents_mod, "_get_agent_registry", return_value=local_registry):
            result = executor._agents._exec_wait_for_any_agent(
                {"agent_ids": ["sa-a", "sa-missing"], "timeout": 5}
            )
        assert result["success"] is False
        assert result["status"] == "not_found"
        assert "sa-missing" in result["error"]

    def test_empty_agent_ids_rejected(self, executor, local_registry):
        with patch.object(agents_mod, "_get_agent_registry", return_value=local_registry):
            result = executor._agents._exec_wait_for_any_agent(
                {"agent_ids": [], "timeout": 5}
            )
        assert result["success"] is False
        assert "non-empty list" in result["error"]

"""Tests for the wait_for_any_agent built-in tool."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from sub_agent_registry import SOURCE_ON_DEMAND, SOURCE_SCHEDULED, SubAgentRecord, SubAgentRegistry
from sub_agent_supervisor import ExecutionOwnership, _OwnedChildState
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
    rec.ownership = ExecutionOwnership(
        origin=source,
        owner_turn_id="r-owner" if source == SOURCE_ON_DEMAND else None,
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
                {"agent_ids": ["sa-a", "sa-b", "sa-c"], "timeout": 5},
                caller_tag="main r-owner",
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
                {"agent_ids": ["sa-a", "sa-b"], "timeout": 5},
                caller_tag="main r-owner",
            )
        assert result["status"] == "done"
        assert result["agent_id"] == "sa-a"

    @pytest.mark.parametrize("status", ["failed", "cancelled"])
    def test_failed_or_cancelled_returned_as_completed(
        self, executor, local_registry, status
    ):
        a = _make_record("sa-a", status=status)
        a.result = f"A-{status}"
        a._result_event.set()
        b = _make_record("sa-b")
        local_registry.register(a)
        local_registry.register(b)

        with patch.object(agents_mod, "_get_agent_registry", return_value=local_registry):
            result = executor._agents._exec_wait_for_any_agent(
                {"agent_ids": ["sa-a", "sa-b"], "timeout": 5},
                caller_tag="main r-owner",
            )
        assert result["status"] == status
        assert result["agent_id"] == "sa-a"
        assert result["result"] == f"A-{status}"
        assert result["success"] is False

    def test_timeout_returns_no_result(self, executor, local_registry):
        a = _make_record("sa-a")
        b = _make_record("sa-b")
        local_registry.register(a)
        local_registry.register(b)

        with patch.object(agents_mod, "_get_agent_registry", return_value=local_registry):
            result = executor._agents._exec_wait_for_any_agent(
                {"agent_ids": ["sa-a", "sa-b"], "timeout": 5},
                caller_tag="main r-owner",
            )
        assert result["status"] == "timeout"
        assert result["success"] is False
        assert "agent_ids" in result

    def test_unknown_agent_id_rejected(self, executor, local_registry):
        a = _make_record("sa-a")
        local_registry.register(a)

        with patch.object(agents_mod, "_get_agent_registry", return_value=local_registry):
            result = executor._agents._exec_wait_for_any_agent(
                {"agent_ids": ["sa-a", "sa-missing"], "timeout": 5},
                caller_tag="main r-owner",
            )
        assert result["success"] is False
        assert result["status"] == "not_found"
        assert "sa-missing" in result["error"]
        assert "no child accessible in this turn" in result["error"]

    @pytest.mark.parametrize(
        ("source", "owner_turn_id"),
        [
            (SOURCE_ON_DEMAND, "r-other"),
            (SOURCE_SCHEDULED, None),
        ],
    )
    def test_get_agent_result_rejects_non_owned_id_without_waiting(
        self, executor, local_registry, source, owner_turn_id
    ):
        record = _make_record("sa-private", source=source)
        record.ownership = ExecutionOwnership(source, owner_turn_id)
        record._result_event.wait = MagicMock(side_effect=AssertionError("must not wait"))
        local_registry.register(record)

        with patch.object(agents_mod, "_get_agent_registry", return_value=local_registry):
            result = executor._agents._exec_get_agent_result(
                {"agent_id": "sa-private", "timeout": 30},
                caller_tag="main r-owner",
            )

        assert result["success"] is False
        assert result["status"] == "not_found"
        assert "sa-private" in result["error"]
        assert "no child accessible in this turn" in result["error"]

    def test_wait_for_any_rejects_non_owned_id_without_waiting(
        self, executor, local_registry
    ):
        owned = _make_record("sa-owned")
        private = _make_record("sa-private")
        private.ownership = ExecutionOwnership(SOURCE_ON_DEMAND, "r-other")
        local_registry.register(owned)
        local_registry.register(private)

        with patch.object(agents_mod, "_get_agent_registry", return_value=local_registry), \
             patch.object(agents_mod.time, "sleep", side_effect=AssertionError("must not wait")):
            result = executor._agents._exec_wait_for_any_agent(
                {"agent_ids": ["sa-owned", "sa-private"], "timeout": 30},
                caller_tag="main r-owner",
            )

        assert result["success"] is False
        assert result["status"] == "not_found"
        assert "sa-private" in result["error"]
        assert "no child accessible in this turn" in result["error"]

    def test_discarded_child_reports_cancelled_without_result_text(
        self, executor, local_registry
    ):
        done = _make_record("sa-done", status="done")
        done.result = "unread secret result"
        done._result_event.set()
        local_registry.register(done)
        executor._supervisor._owned_children[done.agent_id] = _OwnedChildState(done)

        with patch.object(agents_mod, "_get_agent_registry", return_value=local_registry):
            executor._agents._exec_cancel_agent(
                {"agent_id": "sa-done"}, caller_tag="main r-owner"
            )
            result = executor._agents._exec_wait_for_any_agent(
                {"agent_ids": ["sa-done"], "timeout": 5}, caller_tag="main r-owner",
            )

        assert result["status"] == "cancelled"
        assert result["success"] is False
        assert "unread secret result" not in str(result)

    def test_mixed_wait_error_names_every_inaccessible_supplied_id(
        self, executor, local_registry
    ):
        owned = _make_record("sa-owned")
        other = _make_record("sa-other")
        other.ownership = ExecutionOwnership(SOURCE_ON_DEMAND, "r-other")
        scheduled = _make_record("sa-scheduled", source=SOURCE_SCHEDULED)
        for record in (owned, other, scheduled):
            local_registry.register(record)

        with patch.object(agents_mod, "_get_agent_registry", return_value=local_registry), \
             patch.object(agents_mod.time, "sleep", side_effect=AssertionError("must not wait")):
            result = executor._agents._exec_wait_for_any_agent(
                {"agent_ids": ["sa-owned", "sa-other", "sa-scheduled"]},
                caller_tag="main r-owner",
            )

        assert "sa-other" in result["error"]
        assert "sa-scheduled" in result["error"]

    def test_owned_result_resolves_after_registry_completed_ttl(
        self, executor, local_registry
    ):
        record = _make_record("sa-expired", status="done")
        record.result = "retained by turn"
        record._result_event.set()
        executor._supervisor._owned_children[record.agent_id] = _OwnedChildState(record)

        with patch.object(agents_mod, "_get_agent_registry", return_value=local_registry):
            result = executor._agents._exec_get_agent_result(
                {"agent_id": record.agent_id},
                caller_tag="main r-owner",
            )

        assert result["status"] == "done"
        assert result["result"] == "retained by turn"

    def test_empty_agent_ids_rejected(self, executor, local_registry):
        with patch.object(agents_mod, "_get_agent_registry", return_value=local_registry):
            result = executor._agents._exec_wait_for_any_agent(
                {"agent_ids": [], "timeout": 5}
            )
        assert result["success"] is False
        assert "non-empty list" in result["error"]

"""Tests for the cancel_agent built-in tool."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from builtin_tools import agents as agents_mod

import pytest

from sub_agent_registry import (
    SOURCE_DIAGNOSTIC,
    SOURCE_ON_DEMAND,
    SOURCE_PLAN_STEP,
    SOURCE_SCHEDULED,
    SubAgentRecord,
    SubAgentRegistry,
)
from sub_agent_supervisor import ExecutionOwnership, _OwnedChildState


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
    if source in (SOURCE_ON_DEMAND, SOURCE_SCHEDULED):
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


class TestCancelAgent:
    def test_cancel_specific_agent(self, executor, local_registry):
        a = _make_record("sa-a")
        local_registry.register(a)

        with patch.object(agents_mod, "_get_agent_registry", return_value=local_registry):
            result = executor._agents._exec_cancel_agent(
                {"agent_id": "sa-a"}, caller_tag="main r-owner"
            )

        assert result["success"] is True
        assert "sa-a" in result["output"]
        assert a._cancel_event.is_set()

    def test_cancel_all_managed_affects_only_current_turn_owned_children(
        self, executor, local_registry
    ):
        a = _make_record("sa-a", source=SOURCE_ON_DEMAND)
        b = _make_record("sa-b", source=SOURCE_ON_DEMAND, status="done")
        b.result = "discard me"
        b._result_event.set()
        other = _make_record("sa-other", source=SOURCE_ON_DEMAND)
        other.ownership = ExecutionOwnership(SOURCE_ON_DEMAND, "r-other")
        s = _make_record("sa-sched", source=SOURCE_SCHEDULED)
        p = _make_record("sa-plan", source=SOURCE_PLAN_STEP)
        d = _make_record("sa-diag", source=SOURCE_DIAGNOSTIC)
        for r in (a, b, other, s, p, d):
            local_registry.register(r)
        executor._supervisor._owned_children[a.agent_id] = _OwnedChildState(a)
        executor._supervisor._owned_children[b.agent_id] = _OwnedChildState(b)

        with patch.object(agents_mod, "_get_agent_registry", return_value=local_registry):
            result = executor._agents._exec_cancel_agent(
                {"agent_id": "managed"}, caller_tag="main r-owner"
            )

        assert result["success"] is True
        assert result["output"] == "Cancelled 2 turn-owned sub-agent(s)."
        assert a._cancel_event.is_set()
        assert not other._cancel_event.is_set()
        assert not s._cancel_event.is_set()
        assert not p._cancel_event.is_set()
        assert not d._cancel_event.is_set()

        discarded = executor._agents._exec_get_agent_result(
            {"agent_id": "sa-b"}, caller_tag="main r-owner"
        )
        assert discarded["status"] == "cancelled"
        assert "discard" in discarded["output"].lower()
        assert "discard me" not in str(discarded)

    @pytest.mark.parametrize(
        ("agent_id", "record"),
        [
            ("sa-scheduled", _make_record("sa-scheduled", SOURCE_SCHEDULED)),
            ("sa-other", _make_record("sa-other", SOURCE_ON_DEMAND)),
            ("sa-unknown", None),
        ],
    )
    def test_cancel_specific_inaccessible_id_is_uniform(
        self, executor, local_registry, agent_id, record
    ):
        if record is not None:
            if agent_id == "sa-other":
                record.ownership = ExecutionOwnership(SOURCE_ON_DEMAND, "r-other")
            local_registry.register(record)

        with patch.object(agents_mod, "_get_agent_registry", return_value=local_registry):
            result = executor._agents._exec_cancel_agent(
                {"agent_id": agent_id}, caller_tag="main r-owner"
            )

        assert result["success"] is False
        assert agent_id in result["error"]
        assert "no child accessible in this turn" in result["error"]
        if record is not None:
            assert not record._cancel_event.is_set()

    def test_managed_alias_reports_zero_without_target_count_leakage(
        self, executor, local_registry
    ):
        local_registry.register(_make_record("sa-scheduled", SOURCE_SCHEDULED))
        other = _make_record("sa-other")
        other.ownership = ExecutionOwnership(SOURCE_ON_DEMAND, "r-other")
        local_registry.register(other)

        with patch.object(agents_mod, "_get_agent_registry", return_value=local_registry):
            result = executor._agents._exec_cancel_agent(
                {"agent_id": "all"}, caller_tag="main r-owner"
            )

        assert result["success"] is True
        assert result["output"] == "Cancelled 0 turn-owned sub-agent(s)."

    def test_operator_managed_cancellation_scope_includes_scheduled(self, local_registry):
        on_demand = _make_record("sa-on-demand", source=SOURCE_ON_DEMAND)
        scheduled = _make_record("sa-scheduled", source=SOURCE_SCHEDULED)
        plan = _make_record("sa-plan", source=SOURCE_PLAN_STEP)
        for record in (on_demand, scheduled, plan):
            local_registry.register(record)

        cancelled = local_registry.cancel_all_managed()

        assert cancelled == 2
        assert on_demand._cancel_event.is_set()
        assert scheduled._cancel_event.is_set()
        assert not plan._cancel_event.is_set()

    def test_not_confirmation_gated(self, executor, local_registry):
        a = _make_record("sa-a")
        local_registry.register(a)
        confirm_prompt = MagicMock()
        executor._subagent_confirm_prompt_fn = confirm_prompt

        with patch.object(agents_mod, "_get_agent_registry", return_value=local_registry):
            executor._agents._exec_cancel_agent(
                {"agent_id": "sa-a"}, caller_tag="main r-owner"
            )

        assert confirm_prompt.called is False

    def test_unknown_agent_id_handled(self, executor, local_registry):
        with patch.object(agents_mod, "_get_agent_registry", return_value=local_registry):
            result = executor._agents._exec_cancel_agent(
                {"agent_id": "sa-missing"}, caller_tag="main r-owner"
            )

        assert result["success"] is False
        assert "sa-missing" in result["error"]

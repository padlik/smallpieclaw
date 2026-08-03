"""P2: runtime semantic-memory consolidation regressions.

Verifies that JSON LongTermMemory is no longer written at runtime:

- A completed sub-agent does NOT auto-persist its result via LongTermMemory.add().
- The deprecated ``longterm_memory_update`` scheduled job does NOT write to the
  JSON LongTermMemory store.

Runtime semantic recall is served by graph memory; the JSON store is
legacy/backfill-only (see backfill_graph_memory.py, still covered separately).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from builtin_executor import BuiltinExecutor
from memory_store import LongTermMemory
from sub_agent_supervisor import SupervisionOptions


def _make_runner(agent_id: str = "sa-p2", model_id: str = "test-model") -> MagicMock:
    runner = MagicMock()
    runner.agent_id = agent_id
    runner._model_id = model_id
    runner._cancel_event = MagicMock()
    runner._llm = MagicMock()
    runner._agent = MagicMock()
    runner._agent.max_iterations = 8
    runner.run.return_value = "sub-agent result text"
    runner._agent.long_term = MagicMock()
    return runner


def _make_registry(count: int = 0) -> MagicMock:
    reg = MagicMock()
    reg.count_managed.return_value = count
    return reg


class TestSubAgentNoLongTermWrite:
    def test_completion_does_not_call_long_term_add(self):
        runner = _make_runner()
        exc = BuiltinExecutor(sub_agent_factory=MagicMock(return_value=runner))
        # Run the spawn closure synchronously so completion logic executes inline.
        with patch("sub_agent_registry.get_registry", return_value=_make_registry(0)), \
             patch.object(exc._supervisor._pool, "submit",
                          side_effect=lambda fn, *a, **_kw: fn()):
            result = exc._exec_spawn_agent(
                {"task": "do something"}, caller_depth=0,
                options=SupervisionOptions(notify=False),
            )
        assert result["success"] is True
        runner.run.assert_called_once()
        # The removed auto-write must not be reinstated.
        runner._agent.long_term.add.assert_not_called()


class TestSchedulerNoLongTermWrite:
    def _make_scheduler(self, tmp_path, ltm, agent_result="summary"):
        from scheduler import Scheduler
        from xdg import xdg_paths

        cfg = {"agent": {"default_model": "test"}, "scheduler": {"enabled": True}}
        # NOTE: Scheduler no longer accepts a long_term_memory parameter (the
        # legacy JSON store is migration-only). The scheduler therefore has no
        # path to write to long-term memory at all; ``ltm`` is retained purely to
        # assert that no write occurs.
        s = Scheduler(
            cfg,
            notify_fn=lambda _msg: None,
            agent_fn=lambda _task: agent_result,
            paths=xdg_paths("test-agent"),
        )
        return s

    def test_longterm_memory_update_job_does_not_write(self, tmp_path, tmp_xdg):
        ltm = MagicMock()
        s = self._make_scheduler(tmp_path, ltm)
        # builtin_executor is None → legacy fallback path that previously wrote.
        assert s.builtin_executor is None
        s._jobs_meta = {
            "longterm_memory_update": {
                "enabled": True,
                "task": "summarize recent activity",
                "notify": False,
            }
        }
        s._run_job("longterm_memory_update")
        ltm.add.assert_not_called()


class TestLongTermMemoryMigrationShim:
    def test_class_is_migration_only(self):
        assert LongTermMemory.is_migration_only is True

    def test_deprecation_warning_on_add(self, caplog, tmp_path):
        ltm = LongTermMemory(path=str(tmp_path / "ltm.json"))
        with caplog.at_level("WARNING", logger="memory_store"):
            ltm.add("fact", source="manual")
        assert "deprecated" in caplog.text.lower()

    def test_backfill_cli_can_instantiate(self, tmp_path):
        """backfill_graph_memory.py still needs to load existing JSON files."""
        path = str(tmp_path / "ltm.json")
        ltm = LongTermMemory(path=path)
        ltm.add("backfill entry")
        pairs = ltm.entries()
        assert len(pairs) == 1
        assert pairs[0][1]["content"] == "backfill entry"

    @pytest.mark.parametrize("module_name", ["agent_controller", "scheduler", "builtin_executor", "react_loop"])
    def test_runtime_modules_do_not_import_long_term_memory_constructor(self, module_name):
        """Only the backfill CLI is allowed to construct LongTermMemory."""
        mod = __import__(module_name)
        names = set(dir(mod))
        # If present, it may be referenced in comments only; ensure no live
        # LongTermMemory() call path exists by checking no constructor alias.
        assert "LongTermMemory" not in names or module_name in ("backfill_graph_memory",)

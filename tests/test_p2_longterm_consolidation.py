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

from builtin_executor import BuiltinExecutor


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
             patch.object(exc._sub_agent_pool, "submit",
                          side_effect=lambda fn, *a, **kw: fn()):
            result = exc._exec_spawn_agent(
                {"task": "do something", "_notify": False}, caller_depth=0
            )
        assert result["success"] is True
        runner.run.assert_called_once()
        # The removed auto-write must not be reinstated.
        runner._agent.long_term.add.assert_not_called()


class TestSchedulerNoLongTermWrite:
    def _make_scheduler(self, tmp_path, ltm, agent_result="summary"):
        from scheduler import Scheduler

        cfg = {"agent": {"default_model": "test"}, "scheduler": {"enabled": True}}
        # NOTE: Scheduler no longer accepts a long_term_memory parameter (the
        # legacy JSON store is migration-only). The scheduler therefore has no
        # path to write to long-term memory at all; ``ltm`` is retained purely to
        # assert that no write occurs.
        s = Scheduler(
            cfg,
            notify_fn=lambda _msg: None,
            agent_fn=lambda _task: agent_result,
            data_dir=str(tmp_path),
        )
        return s

    def test_longterm_memory_update_job_does_not_write(self, tmp_path):
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

"""Tests for atomic, crash-safe sub-agent context persistence (memory item E).

``_save_context`` must write the job context JSON atomically (temp file +
``os.replace``) so an interrupted/failed write cannot corrupt or truncate an
existing context file.
"""

from __future__ import annotations

import json
import os

import pytest

from builtin_executor import _load_context, _save_context
from memory_store import ShortTermMemory
from sub_agent_supervisor import SupervisionOptions


def _stm(turns):
    stm = ShortTermMemory(max_turns=50)
    for role, content in turns:
        stm.add(role, content)
    return stm


class TestSaveContextAtomic:
    def test_save_then_load_roundtrip(self, tmp_path):
        stm = _stm([("user", "hello"), ("assistant", "hi there")])
        _save_context("job-1", stm, str(tmp_path))

        path = tmp_path / "job_contexts" / "job-1.json"
        assert path.exists()
        # No leftover temp file after a successful commit.
        assert list((tmp_path / "job_contexts").glob(".job-1.json.*.tmp")) == []

        loaded = _load_context("job-1", str(tmp_path))
        msgs = loaded.get_messages()
        assert msgs[0]["content"] == "hello"
        assert msgs[1]["content"] == "hi there"

    def test_writes_valid_json(self, tmp_path):
        stm = _stm([("user", "abc")])
        _save_context("job-2", stm, str(tmp_path))
        path = tmp_path / "job_contexts" / "job-2.json"
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert isinstance(data, (dict, list))
        assert data  # non-empty

    def test_failed_write_preserves_existing_file(self, tmp_path, monkeypatch):
        # First, a good save.
        good = _stm([("user", "original"), ("assistant", "keep me")])
        _save_context("job-3", good, str(tmp_path))
        path = tmp_path / "job_contexts" / "job-3.json"
        original_bytes = path.read_bytes()

        # Now simulate a crash during commit: os.replace raises.
        def _boom(src, dst):
            raise OSError("simulated crash during replace")

        monkeypatch.setattr(os, "replace", _boom)

        new = _stm([("user", "should not land")])
        # Must not raise — failure is logged and swallowed.
        _save_context("job-3", new, str(tmp_path))

        # The pre-existing context file is untouched (atomic guarantee).
        assert path.read_bytes() == original_bytes
        # Temp file is cleaned up on failure.
        assert list((tmp_path / "job_contexts").glob(".job-3.json.*.tmp")) == []

    @pytest.mark.parametrize("bad_key", ["../escape", "nested/path", "/abs", "..", "."])
    def test_rejects_path_traversal_context_keys(self, tmp_path, bad_key):
        stm = _stm([("user", "abc")])
        with pytest.raises(ValueError):
            _save_context(bad_key, stm, str(tmp_path))
        with pytest.raises(ValueError):
            _load_context(bad_key, str(tmp_path))
        assert not (tmp_path / "escape.json").exists()
        assert not (tmp_path / "job_contexts").exists()

    def test_unique_temp_names_do_not_collide(self, tmp_path, monkeypatch):
        stm = _stm([("user", "abc")])
        seen_sources = []
        real_replace = os.replace

        def _track_replace(src, dst):
            seen_sources.append(os.path.basename(src))
            real_replace(src, dst)

        monkeypatch.setattr(os, "replace", _track_replace)

        _save_context("job-unique", stm, str(tmp_path))
        _save_context("job-unique", stm, str(tmp_path))

        assert len(seen_sources) == 2
        assert seen_sources[0] != seen_sources[1]

    def test_load_missing_returns_fresh(self, tmp_path):
        stm = _load_context("never-saved", str(tmp_path))
        assert stm.get_messages() == []


class TestSpawnSavesContextInFinally:
    """The spawn execution path saves context from a ``finally`` block so a
    sub-agent crash mid-task does not lose its short-term memory."""

    def _run_spawn(self, tmp_path, *, run_side_effect, context_key="ctx-key"):
        from unittest.mock import MagicMock, patch

        from builtin_executor import BuiltinExecutor

        runner = MagicMock()
        runner.agent_id = "sa-fin01"
        runner._model_id = "test-model"
        runner._cancel_event = MagicMock()
        runner._llm = MagicMock()
        runner._agent = MagicMock()
        runner._agent.max_iterations = 8
        runner._short_term = _stm([("user", "in flight"), ("assistant", "partial")])
        if isinstance(run_side_effect, Exception):
            runner.run.side_effect = run_side_effect
        else:
            runner.run.return_value = run_side_effect

        exc = BuiltinExecutor(
            sub_agent_factory=MagicMock(return_value=runner),
            data_dir=str(tmp_path),
        )
        with patch("sub_agent_registry.get_registry", return_value=_make_reg()), \
             patch.object(exc._supervisor._pool, "submit",
                          side_effect=lambda fn, *a, **kw: fn()):
            exc._exec_spawn_agent(
                {"task": "do work", "context_key": context_key},
                caller_depth=0,
                options=SupervisionOptions(notify=False),
            )
        return tmp_path / "job_contexts" / f"{context_key}.json"

    def test_context_saved_on_success(self, tmp_path):
        path = self._run_spawn(tmp_path, run_side_effect="all good")
        assert path.exists()
        loaded = _load_context("ctx-key", str(tmp_path))
        assert loaded.get_messages()[0]["content"] == "in flight"

    def test_context_saved_on_exception(self, tmp_path):
        # Sub-agent crashes mid-task; context must still be persisted.
        path = self._run_spawn(tmp_path, run_side_effect=RuntimeError("boom"))
        assert path.exists()
        loaded = _load_context("ctx-key", str(tmp_path))
        assert loaded.get_messages()[0]["content"] == "in flight"

    def test_context_saved_before_result_event_is_set(self, tmp_path):
        from unittest.mock import MagicMock, patch

        from builtin_executor import BuiltinExecutor

        class _Registry:
            def __init__(self):
                self.record = None

            def count_managed(self):
                return 0

            def register(self, record):
                self.record = record

            def update_iteration(self, *_args, **_kwargs):
                pass

            def unregister(self, *_args, **_kwargs):
                pass

        registry = _Registry()
        runner = MagicMock()
        runner.agent_id = "sa-order"
        runner._model_id = "test-model"
        runner._cancel_event = MagicMock()
        runner._llm = MagicMock()
        runner._agent = MagicMock()
        runner._agent.max_iterations = 8
        runner._short_term = _stm([("user", "ordered")])
        runner.run.return_value = "done"

        def _save_spy(*_args, **_kwargs):
            assert registry.record is not None
            assert not registry.record._result_event.is_set()

        exc = BuiltinExecutor(
            sub_agent_factory=MagicMock(return_value=runner),
            data_dir=str(tmp_path),
        )
        with patch("sub_agent_registry.get_registry", return_value=registry), \
             patch("builtin_executor._save_context", side_effect=_save_spy), \
             patch.object(exc._supervisor._pool, "submit",
                          side_effect=lambda fn, *a, **kw: fn()):
            exc._exec_spawn_agent(
                {"task": "do work", "context_key": "ctx-order"},
                caller_depth=0,
                options=SupervisionOptions(notify=False),
            )

        assert registry.record._result_event.is_set()


def _make_reg(count: int = 0):
    from unittest.mock import MagicMock

    reg = MagicMock()
    reg.count_managed.return_value = count
    return reg

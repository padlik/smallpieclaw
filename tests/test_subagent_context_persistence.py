"""Tests for atomic, crash-safe sub-agent context persistence (memory item E).

``_save_context`` must write the job context JSON atomically (temp file +
``os.replace``) so an interrupted/failed write cannot corrupt or truncate an
existing context file.
"""

from __future__ import annotations

import json
import os

import pytest

from builtin_executor import _save_context
from builtin_tools.context_io import _load_context
from memory_store import ShortTermMemory
from sub_agent_supervisor import SupervisionOptions
from tests.inline_futures import run_inline_future


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

    def _run_spawn(self, make_builtin_executor, tmp_path, *, run_side_effect, context_key="ctx-key"):
        from unittest.mock import MagicMock, patch


        runner = MagicMock()
        runner.agent_id = "sa-fin01"
        runner._model_id = "test-model"
        runner.model_id = "test-model"
        runner._cancel_event = MagicMock()
        runner._llm = MagicMock()
        runner._agent = MagicMock()
        runner._agent.max_iterations = 8
        runner._agent._trace_id = None
        runner.trace_id = None
        runner._short_term = _stm([("user", "in flight"), ("assistant", "partial")])
        runner.short_term = runner._short_term
        if isinstance(run_side_effect, Exception):
            runner.run.side_effect = run_side_effect
        else:
            runner.run.return_value = run_side_effect

        exc = make_builtin_executor(
            sub_agent_factory=MagicMock(return_value=runner),
            data_dir=str(tmp_path),
        )
        with patch("sub_agent_registry.get_registry", return_value=_make_reg()), \
             patch.object(exc._supervisor._pool, "submit",
                          side_effect=run_inline_future):
            exc._exec_spawn_agent(
                {"task": "do work", "context_key": context_key},
                caller_depth=0,
                options=SupervisionOptions(notify=False),
            )
        return tmp_path / "job_contexts" / f"{context_key}.json"

    def test_context_saved_on_success(self, make_builtin_executor, tmp_path):
        path = self._run_spawn(make_builtin_executor, tmp_path, run_side_effect="all good")
        assert path.exists()
        loaded = _load_context("ctx-key", str(tmp_path))
        assert loaded.get_messages()[0]["content"] == "in flight"

    def test_context_saved_on_exception(self, make_builtin_executor, tmp_path):
        # Sub-agent crashes mid-task; context must still be persisted.
        path = self._run_spawn(make_builtin_executor, tmp_path, run_side_effect=RuntimeError("boom"))
        assert path.exists()
        loaded = _load_context("ctx-key", str(tmp_path))
        assert loaded.get_messages()[0]["content"] == "in flight"

    def test_context_saved_before_result_event_is_set(self, make_builtin_executor, tmp_path):
        from unittest.mock import MagicMock, patch


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
        runner.model_id = "test-model"
        runner._cancel_event = MagicMock()
        runner._llm = MagicMock()
        runner._agent = MagicMock()
        runner._agent.max_iterations = 8
        runner._agent._trace_id = None
        runner.trace_id = None
        runner._short_term = _stm([("user", "ordered")])
        runner.short_term = runner._short_term
        runner.run.return_value = "done"

        def _save_spy(*_args, **_kwargs):
            assert registry.record is not None
            assert not registry.record._result_event.is_set()

        exc = make_builtin_executor(
            sub_agent_factory=MagicMock(return_value=runner),
            data_dir=str(tmp_path),
        )
        with patch("sub_agent_registry.get_registry", return_value=registry), \
             patch("builtin_tools.context_io._save_context", side_effect=_save_spy) as _save_mock, \
             patch.object(exc._supervisor._pool, "submit",
                          side_effect=run_inline_future):
            exc._exec_spawn_agent(
                {"task": "do work", "context_key": "ctx-order"},
                caller_depth=0,
                options=SupervisionOptions(notify=False),
            )

        assert _save_mock.called, "context_io._save_context must be invoked (patch not a silent no-op)"
        assert registry.record._result_event.is_set()


def _make_reg(count: int = 0):
    from unittest.mock import MagicMock

    reg = MagicMock()
    reg.count_managed.return_value = count
    return reg


class TestScheduledContextOriginIsolation:
    """Scheduled preserved context lives in a namespace on-demand keys cannot reach."""

    def test_scheduled_round_trip_returns_own_conversation(self, tmp_path):
        from builtin_tools.context_io import scheduled_context_key

        key = scheduled_context_key("nightly")
        _save_context(key, _stm([("user", "mine"), ("assistant", "yes")]), str(tmp_path))

        loaded = _load_context(key, str(tmp_path))

        assert [m["content"] for m in loaded.get_messages()] == ["mine", "yes"]

    def test_scheduled_context_is_stored_in_scheduled_subdirectory(self, tmp_path):
        from builtin_tools.context_io import scheduled_context_key

        _save_context(scheduled_context_key("nightly"), _stm([("user", "x")]), str(tmp_path))

        files = list((tmp_path / "job_contexts" / "scheduled").glob("*.json"))
        assert len(files) == 1
        assert list((tmp_path / "job_contexts").glob("*.json")) == []

    def test_on_demand_key_equal_to_tag_cannot_read_scheduled_context(self, tmp_path):
        from builtin_tools.context_io import scheduled_context_key

        _save_context(scheduled_context_key("nightly"), _stm([("user", "secret job")]), str(tmp_path))

        assert _load_context("nightly", str(tmp_path)).get_messages() == []

    def test_on_demand_key_equal_to_tag_cannot_overwrite_scheduled_context(self, tmp_path):
        from builtin_tools.context_io import scheduled_context_key

        key = scheduled_context_key("nightly")
        _save_context(key, _stm([("user", "job turn")]), str(tmp_path))

        _save_context("nightly", _stm([("user", "on-demand turn")]), str(tmp_path))

        assert _load_context(key, str(tmp_path)).get_messages()[0]["content"] == "job turn"

    def test_scheduled_job_cannot_read_on_demand_context_with_same_name(self, tmp_path):
        from builtin_tools.context_io import scheduled_context_key

        _save_context("nightly", _stm([("user", "on-demand turn")]), str(tmp_path))

        assert _load_context(scheduled_context_key("nightly"), str(tmp_path)).get_messages() == []

    def test_scheduled_job_cannot_overwrite_on_demand_context_with_same_name(self, tmp_path):
        from builtin_tools.context_io import scheduled_context_key

        _save_context("nightly", _stm([("user", "on-demand turn")]), str(tmp_path))

        _save_context(scheduled_context_key("nightly"), _stm([("user", "job turn")]), str(tmp_path))

        assert _load_context("nightly", str(tmp_path)).get_messages()[0]["content"] == "on-demand turn"

    def test_tags_that_normalize_identically_do_not_share_context(self, tmp_path):
        from builtin_tools.context_io import _context_path, scheduled_context_key
        from scheduler import _normalize_context_key

        first, second = "Daily-Report", "daily report"
        assert _normalize_context_key(first) == _normalize_context_key(second)

        assert _context_path(scheduled_context_key(first), str(tmp_path)) != _context_path(
            scheduled_context_key(second), str(tmp_path),
        )

    def test_scheduled_context_key_is_deterministic(self):
        from builtin_tools.context_io import scheduled_context_key

        assert scheduled_context_key("nightly") == scheduled_context_key("nightly")

    def test_overlong_tag_still_yields_loadable_key(self, tmp_path):
        from builtin_tools.context_io import scheduled_context_key

        key = scheduled_context_key("t" * 500)
        _save_context(key, _stm([("user", "long")]), str(tmp_path))

        assert _load_context(key, str(tmp_path)).get_messages()[0]["content"] == "long"

    def test_on_demand_validator_rejects_scheduled_key(self):
        from builtin_tools.context_io import _validate_context_key, scheduled_context_key

        with pytest.raises(ValueError):
            _validate_context_key(scheduled_context_key("nightly"))

    @pytest.mark.parametrize("bad", ["scheduled/../x", "scheduled/a/b", "scheduled/", "scheduled/UP-0123456789abcdef"])
    def test_malformed_scheduled_keys_are_rejected(self, tmp_path, bad):
        with pytest.raises(ValueError):
            _load_context(bad, str(tmp_path))


class TestScheduledSpawnUsesNamespacedKey:
    """The scheduled spawn path ignores any model-supplied context_key value."""

    def _spawn(self, make_builtin_executor, tmp_path, *, args, options):
        from unittest.mock import MagicMock, patch

        runner = MagicMock()
        runner.agent_id = "sa-ns01"
        runner.model_id = runner._model_id = "test-model"
        runner._agent = MagicMock()
        runner._agent.max_iterations = 8
        runner._agent._trace_id = None
        runner.trace_id = None
        runner.short_term = _stm([("user", "job in flight")])
        runner.run.return_value = "done"
        factory = MagicMock(return_value=runner)
        exc = make_builtin_executor(sub_agent_factory=factory, data_dir=str(tmp_path))
        with patch("sub_agent_registry.get_registry", return_value=_make_reg()), \
             patch.object(exc._supervisor._pool, "submit",
                          side_effect=run_inline_future):
            exc._exec_spawn_agent(args, caller_depth=0, options=options)
        return factory

    def test_scheduled_spawn_passes_namespaced_key_to_factory(self, make_builtin_executor, tmp_path):
        from builtin_tools.context_io import scheduled_context_key

        factory = self._spawn(
            make_builtin_executor, tmp_path,
            args={"task": "t", "context_key": "nightly"},
            options=SupervisionOptions(job_tag="nightly", source="scheduled", notify=False),
        )

        assert factory.call_args.kwargs["context_key"] == scheduled_context_key("nightly")

    def test_scheduled_spawn_ignores_model_supplied_context_key(self, make_builtin_executor, tmp_path):
        from builtin_tools.context_io import scheduled_context_key

        factory = self._spawn(
            make_builtin_executor, tmp_path,
            args={"task": "t", "context_key": "victim-key"},
            options=SupervisionOptions(job_tag="nightly", source="scheduled", notify=False),
        )

        assert factory.call_args.kwargs["context_key"] == scheduled_context_key("nightly")

    def test_scheduled_spawn_saves_under_scheduled_namespace(self, make_builtin_executor, tmp_path):
        self._spawn(
            make_builtin_executor, tmp_path,
            args={"task": "t", "context_key": "nightly"},
            options=SupervisionOptions(job_tag="nightly", source="scheduled", notify=False),
        )

        assert len(list((tmp_path / "job_contexts" / "scheduled").glob("*.json"))) == 1
        assert not (tmp_path / "job_contexts" / "nightly.json").exists()

    def test_scheduled_spawn_without_marker_persists_nothing(self, make_builtin_executor, tmp_path):
        factory = self._spawn(
            make_builtin_executor, tmp_path,
            args={"task": "t"},
            options=SupervisionOptions(job_tag="nightly", source="scheduled", notify=False),
        )

        assert factory.call_args.kwargs["context_key"] is None

    def test_on_demand_spawn_keeps_model_supplied_key(self, make_builtin_executor, tmp_path):
        factory = self._spawn(
            make_builtin_executor, tmp_path,
            args={"task": "t", "context_key": "my-key"},
            options=SupervisionOptions(notify=False),
        )

        assert factory.call_args.kwargs["context_key"] == "my-key"
        assert (tmp_path / "job_contexts" / "my-key.json").exists()

    def test_on_demand_spawn_cannot_name_a_scheduled_key(self, make_builtin_executor, tmp_path):
        from builtin_tools.context_io import scheduled_context_key

        result = {}
        from unittest.mock import MagicMock, patch
        exc = make_builtin_executor(sub_agent_factory=MagicMock(), data_dir=str(tmp_path))
        with patch("sub_agent_registry.get_registry", return_value=_make_reg()):
            result = exc._exec_spawn_agent(
                {"task": "t", "context_key": scheduled_context_key("nightly")},
                caller_depth=0, options=SupervisionOptions(notify=False),
            )

        assert result["success"] is False
        assert "invalid context_key" in result["error"]

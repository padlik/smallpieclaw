"""Tests for PromptRegistry: ID assignment, persistence, reload, and ordering."""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from prompt_registry import PromptRegistry


@pytest.fixture
def registry(tmp_path):
    """Create a registry backed by a temporary data directory."""
    return PromptRegistry(data_dir=str(tmp_path))


class TestCancelledStatus:
    """W2 regression: a '[Cancelled]' agent result must be recorded as 'cancelled', not 'done'.

    Tests import _classify_final_status from telegram_interface so that reverting
    telegram_interface.py:626 (or the helper at :82) would fail this suite.
    """

    def test_cancelled_sentinel_maps_to_cancelled(self):
        from telegram_interface import _classify_final_status
        assert _classify_final_status("[Cancelled]") == "cancelled"

    def test_normal_result_maps_to_done(self):
        from telegram_interface import _classify_final_status
        assert _classify_final_status("Here is your answer.") == "done"

    def test_empty_result_maps_to_done(self):
        from telegram_interface import _classify_final_status
        assert _classify_final_status("") == "done"

    def test_cancelled_result_stored_in_registry(self, registry):
        from telegram_interface import _classify_final_status
        rec = registry.start("r-w2a", "task a")
        registry.finish(rec.prompt_id, _classify_final_status("[Cancelled]"))
        assert registry.get(rec.prompt_id).status == "cancelled"

    def test_normal_result_stored_in_registry(self, registry):
        from telegram_interface import _classify_final_status
        rec = registry.start("r-w2b", "task b")
        registry.finish(rec.prompt_id, _classify_final_status("Here is your answer."))
        assert registry.get(rec.prompt_id).status == "done"


class TestIdAssignment:
    def test_sequential_ids(self, registry):
        r1 = registry.start("r-aaaa", "first")
        r2 = registry.start("r-bbbb", "second")
        r3 = registry.start("r-cccc", "third")
        assert r1.prompt_id == 1
        assert r2.prompt_id == 2
        assert r3.prompt_id == 3


class TestPersistence:
    def test_start_appends_record(self, registry, tmp_path):
        record = registry.start("r-1111", "hello world")
        path = os.path.join(str(tmp_path), "prompts.jsonl")
        with open(path, encoding="utf-8") as f:
            lines = [json.loads(line) for line in f]
        assert len(lines) == 1
        assert lines[0]["prompt_id"] == record.prompt_id
        assert lines[0]["trace_id"] == "r-1111"
        assert lines[0]["text"] == "hello world"
        assert lines[0]["status"] == "running"
        assert lines[0]["sub_agent_ids"] == []

    def test_add_sub_agent_appends_update(self, registry, tmp_path):
        record = registry.start("r-2222", "task")
        registry.add_sub_agent(record.prompt_id, "sa-abc123")
        path = os.path.join(str(tmp_path), "prompts.jsonl")
        with open(path, encoding="utf-8") as f:
            lines = [json.loads(line) for line in f]
        assert len(lines) == 2
        assert lines[1]["action"] == "add_sub_agent"
        assert lines[1]["agent_id"] == "sa-abc123"

    def test_finish_appends_finalization(self, registry, tmp_path):
        record = registry.start("r-3333", "task")
        registry.add_sub_agent(record.prompt_id, "sa-xyz")
        time.sleep(0.01)
        registry.finish(record.prompt_id, "done")
        path = os.path.join(str(tmp_path), "prompts.jsonl")
        with open(path, encoding="utf-8") as f:
            lines = [json.loads(line) for line in f]
        assert len(lines) == 3
        assert lines[2]["action"] == "finish"
        assert lines[2]["status"] == "done"
        assert lines[2]["sub_agent_ids"] == ["sa-xyz"]
        assert lines[2]["ended_at"] >= record.started_at


class TestReload:
    def test_reload_recovers_state(self, tmp_path):
        reg1 = PromptRegistry(data_dir=str(tmp_path))
        r1 = reg1.start("r-reload-a", "first task")
        reg1.add_sub_agent(r1.prompt_id, "sa-1")
        reg1.finish(r1.prompt_id, "done")
        reg1.start("r-reload-b", "second task")

        reg2 = PromptRegistry(data_dir=str(tmp_path))
        assert reg2._next_id == 3
        recovered = reg2.get(1)
        assert recovered is not None
        assert recovered.trace_id == "r-reload-a"
        assert recovered.status == "done"
        assert recovered.sub_agent_ids == ["sa-1"]
        assert reg2.get(2).status == "running"  # type: ignore[union-attr]
        assert reg2.by_trace("r-reload-a") == recovered

    def test_reload_empty_directory_starts_at_one(self, tmp_path):
        registry = PromptRegistry(data_dir=str(tmp_path))
        record = registry.start("r-first", "first")
        assert record.prompt_id == 1


class TestListRecent:
    def test_most_recent_first(self, registry):
        for i in range(5):
            registry.start(f"r-{i}", f"task {i}")
        recent = registry.list_recent(n=3)
        assert [r.prompt_id for r in recent] == [5, 4, 3]

    def test_limit_respected(self, registry):
        registry.start("r-1", "a")
        registry.start("r-2", "b")
        assert len(registry.list_recent(n=1)) == 1


class TestThreadSafety:
    def test_concurrent_start_finish(self, registry):
        started_ids = []
        lock = threading.Lock()

        def worker(i):
            record = registry.start(f"r-thread-{i}", f"task {i}")
            with lock:
                started_ids.append(record.prompt_id)
            registry.finish(record.prompt_id, "done")

        with ThreadPoolExecutor(max_workers=20) as pool:
            for i in range(50):
                pool.submit(worker, i)

        assert len(started_ids) == 50
        assert len(set(started_ids)) == 50
        assert all(registry.get(pid).status == "done" for pid in started_ids)  # type: ignore[union-attr]


class TestGetAndByTrace:
    def test_get_unknown_returns_none(self, registry):
        assert registry.get(999) is None

    def test_by_trace_unknown_returns_none(self, registry):
        assert registry.by_trace("r-missing") is None

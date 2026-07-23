"""Tests for PromptRegistry: ID assignment, persistence, reload, and ordering."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from prompt_registry import _generate_ulid, PromptRegistry

_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _assert_ulid(value: str) -> None:
    """Assert *value* looks like a 26-char Crockford base32 ULID."""
    assert isinstance(value, str)
    assert len(value) == 26
    assert all(ch in _CROCKFORD_ALPHABET for ch in value)


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


class TestUlidGenerator:
    def test_ulid_format_and_charset(self):
        ulid = _generate_ulid()
        _assert_ulid(ulid)

    def test_ulid_timestamp_prefix_increases_across_milliseconds(self):
        a = _generate_ulid()
        time.sleep(0.002)
        b = _generate_ulid()
        # ULIDs are lexicographically sortable by timestamp.
        assert b > a

    def test_same_millisecond_calls_differ_in_random_suffix(self):
        # Loop quickly to get two ULIDs in the same ms.
        first = _generate_ulid()
        second = _generate_ulid()
        attempts = 0
        while first[:10] != second[:10] and attempts < 100:
            first = _generate_ulid()
            second = _generate_ulid()
            attempts += 1
        assert first[:10] == second[:10], "could not get two ULIDs in the same ms"
        assert first != second
        assert first[10:] != second[10:]


class TestIdAssignment:
    def test_ids_are_ulids(self, registry):
        r1 = registry.start("r-aaaa", "first")
        r2 = registry.start("r-bbbb", "second")
        r3 = registry.start("r-cccc", "third")
        _assert_ulid(r1.prompt_id)
        _assert_ulid(r2.prompt_id)
        _assert_ulid(r3.prompt_id)
        assert r1.prompt_id != r2.prompt_id != r3.prompt_id

    def test_start_log_uses_string_format(self, registry, caplog):
        caplog.set_level(logging.INFO, logger="prompt_registry")
        record = registry.start("r-log-start", "start task")
        messages = [r.getMessage() for r in caplog.records if r.name == "prompt_registry"]
        assert any(record.prompt_id in msg for msg in messages)


class TestPersistence:
    def test_start_appends_record(self, registry, tmp_path):
        record = registry.start("r-1111", "hello world")
        path = os.path.join(str(tmp_path), "prompts.jsonl")
        with open(path, encoding="utf-8") as f:
            lines = [json.loads(line) for line in f]
        assert len(lines) == 1
        assert lines[0]["prompt_id"] == record.prompt_id
        _assert_ulid(lines[0]["prompt_id"])
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

    def test_finish_appends_finalization(self, registry, tmp_path, caplog):
        caplog.set_level(logging.INFO, logger="prompt_registry")
        record = registry.start("r-3333", "task")
        registry.add_sub_agent(record.prompt_id, "sa-xyz")
        time.sleep(0.01)
        registry.finish(record.prompt_id, "done")

        # Regression guard: getMessage() would raise if a %d format remained.
        messages = [r.getMessage() for r in caplog.records if r.name == "prompt_registry"]
        assert any(record.prompt_id in msg for msg in messages)

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
        r2 = reg1.start("r-reload-b", "second task")

        reg2 = PromptRegistry(data_dir=str(tmp_path))
        recovered1 = reg2.get(r1.prompt_id)
        assert recovered1 is not None
        assert recovered1.trace_id == "r-reload-a"
        assert recovered1.status == "done"
        assert recovered1.sub_agent_ids == ["sa-1"]

        recovered2 = reg2.get(r2.prompt_id)
        assert recovered2 is not None
        assert recovered2.status == "running"

        assert reg2.by_trace("r-reload-a") == recovered1

    def test_reload_empty_directory_gets_ulid(self, tmp_path):
        registry = PromptRegistry(data_dir=str(tmp_path))
        record = registry.start("r-first", "first")
        _assert_ulid(record.prompt_id)


class TestListRecent:
    def test_most_recent_first(self, registry):
        for i in range(5):
            registry.start(f"r-{i}", f"task {i}")
        recent = registry.list_recent(n=3)
        ids = [r.prompt_id for r in recent]
        assert len(ids) == 3
        assert all(isinstance(pid, str) for pid in ids)

    def test_sorts_by_started_at_descending(self, registry):
        # Manually create records with controlled started_at values.
        base = time.time()
        early_id = "01EARLYID0000000000000001"
        late_id = "01LATEID0000000000000002"
        from prompt_registry import PromptRecord
        registry._records[early_id] = PromptRecord(
            prompt_id=early_id,
            trace_id="r-early",
            text="early task",
            started_at=base - 10.0,
        )
        registry._records[late_id] = PromptRecord(
            prompt_id=late_id,
            trace_id="r-late",
            text="late task",
            started_at=base,
        )
        recent = registry.list_recent(n=2)
        assert [r.prompt_id for r in recent] == [late_id, early_id]

    def test_mixed_int_and_str_ids_do_not_raise(self, registry):
        from prompt_registry import PromptRecord
        base = time.time()
        registry._records[1] = PromptRecord(
            prompt_id=1,  # type: ignore[arg-type]
            trace_id="r-legacy",
            text="legacy int id",
            started_at=base - 1.0,
        )
        registry._records["01STRID00000000000000001"] = PromptRecord(
            prompt_id="01STRID00000000000000001",
            trace_id="r-str",
            text="string id",
            started_at=base,
        )
        # Must not raise TypeError when sorting by started_at.
        recent = registry.list_recent(n=10)
        assert len(recent) == 2


class TestReplay:
    def test_replay_tolerates_legacy_int_and_new_str_ids(self, tmp_path):
        path = os.path.join(str(tmp_path), "prompts.jsonl")
        legacy_id = 42
        new_id = "01ABCDEF0123456789ABCDEFGH"
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                json.dumps({
                    "prompt_id": legacy_id,
                    "trace_id": "r-legacy",
                    "text": "legacy task",
                    "started_at": time.time() - 1.0,
                    "status": "running",
                    "sub_agent_ids": [],
                }) + "\n"
            )
            f.write(
                json.dumps({
                    "prompt_id": new_id,
                    "trace_id": "r-new",
                    "text": "new task",
                    "started_at": time.time(),
                    "status": "running",
                    "sub_agent_ids": [],
                }) + "\n"
            )
        reg = PromptRegistry(data_dir=str(tmp_path))

        # Replay normalizes legacy int IDs to str, so a string lookup finds it.
        legacy_rec = reg.get(str(legacy_id))
        assert legacy_rec is not None
        assert legacy_rec.prompt_id == str(legacy_id)
        assert legacy_rec.trace_id == "r-legacy"

        new_rec = reg.get(new_id)
        assert new_rec is not None
        assert new_rec.prompt_id == new_id
        assert new_rec.trace_id == "r-new"

    def test_replay_normalizes_legacy_int_id_to_str(self, tmp_path):
        """Legacy integer prompt_id is normalized to str during replay."""
        path = os.path.join(str(tmp_path), "prompts.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                json.dumps({
                    "prompt_id": 1,
                    "trace_id": "r-legacy-1",
                    "text": "legacy int id",
                    "started_at": time.time(),
                    "status": "running",
                    "sub_agent_ids": [],
                }) + "\n"
            )
        registry = PromptRegistry(data_dir=str(tmp_path))

        rec = registry.get("1")
        assert rec is not None
        assert rec.prompt_id == "1"
        assert rec.trace_id == "r-legacy-1"

        assert registry.get(1) is None  # type: ignore[arg-type]

    def test_replay_rejects_bool_and_non_int_str_types(self, tmp_path):
        """Corrupted JSONL with bool/float/list/dict prompt_id is skipped, not stored."""
        path = os.path.join(str(tmp_path), "prompts.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for bad_id in (True, False, 3.14, [1, 2], {"k": "v"}):
                f.write(
                    json.dumps({
                        "prompt_id": bad_id,
                        "trace_id": "r-bad",
                        "text": "bad id",
                        "started_at": time.time(),
                        "status": "running",
                        "sub_agent_ids": [],
                    }) + "\n"
                )
        registry = PromptRegistry(data_dir=str(tmp_path))
        assert registry.list_recent() == []


class TestReset:
    def test_prompt_id_survives_registry_reset(self, tmp_path):
        reg1 = PromptRegistry(data_dir=str(tmp_path))
        r1 = reg1.start("r-reset", "first")

        path = os.path.join(str(tmp_path), "prompts.jsonl")
        os.remove(path)

        reg2 = PromptRegistry(data_dir=str(tmp_path))
        r2 = reg2.start("r-reset-2", "second")

        _assert_ulid(r1.prompt_id)
        _assert_ulid(r2.prompt_id)
        assert r1.prompt_id != r2.prompt_id
        assert reg2.get(r1.prompt_id) is None
        assert reg2.get(r2.prompt_id) is not None


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
        assert registry.get("nonexistent") is None

    def test_by_trace_unknown_returns_none(self, registry):
        assert registry.by_trace("r-missing") is None

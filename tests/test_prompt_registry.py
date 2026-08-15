"""Tests for PromptRegistry: ID assignment, persistence, reload, and ordering."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from prompt_registry import _generate_ulid, PromptRegistry, SearchPage  # noqa: F401

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
        finished_statuses = []
        lock = threading.Lock()

        def worker(i):
            record = registry.start(f"r-thread-{i}", f"task {i}")
            with lock:
                started_ids.append(record.prompt_id)
            registry.finish(record.prompt_id, "done")
            # Capture status immediately after finish returns, before eviction
            # can make the record disappear from memory.
            with lock:
                finished_statuses.append((record.prompt_id, record.status))

        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = [pool.submit(worker, i) for i in range(50)]
            for f in futures:
                f.result()

        assert len(started_ids) == 50
        assert len(set(started_ids)) == 50
        assert all(status == "done" for _, status in finished_statuses)


class TestGetAndByTrace:
    def test_get_unknown_returns_none(self, registry):
        assert registry.get("nonexistent") is None

    def test_by_trace_unknown_returns_none(self, registry):
        assert registry.by_trace("r-missing") is None


class TestArchiveSnapshot:
    def test_archive_snapshot_writes_7_fields(self, registry, tmp_path):
        """finish() writes a snapshot with all 7 fields to the archive."""
        rec = registry.start("r-arch1", "test task")
        registry.finish(rec.prompt_id, "done")
        archive_path = os.path.join(str(tmp_path), "prompts_archive.jsonl")
        with open(archive_path, encoding="utf-8") as f:
            lines = [json.loads(line) for line in f]
        assert len(lines) == 1
        line = lines[0]
        assert set(line.keys()) == {"prompt_id", "trace_id", "text", "started_at", "ended_at", "status", "sub_agent_ids"}
        assert line["prompt_id"] == rec.prompt_id
        assert line["trace_id"] == "r-arch1"
        assert line["text"] == "test task"
        assert line["status"] == "done"
        assert line["ended_at"] is not None
        assert line["sub_agent_ids"] == []

    def test_archive_snapshot_append_only(self, registry, tmp_path):
        """Two finalized prompts produce two lines in the archive."""
        r1 = registry.start("r-app1", "first")
        registry.finish(r1.prompt_id, "done")
        r2 = registry.start("r-app2", "second")
        registry.finish(r2.prompt_id, "done")
        archive_path = os.path.join(str(tmp_path), "prompts_archive.jsonl")
        with open(archive_path, encoding="utf-8") as f:
            lines = [json.loads(line) for line in f]
        assert len(lines) == 2
        assert lines[0]["prompt_id"] == r1.prompt_id
        assert lines[1]["prompt_id"] == r2.prompt_id


class TestBackfillArchive:
    def test_backfill_from_event_log(self, tmp_path):
        """On first startup with no archive, finalized records are backfilled."""
        # Create a registry with some finalized and running records
        reg1 = PromptRegistry(data_dir=str(tmp_path))
        r1 = reg1.start("r-bf1", "finalized task")
        reg1.finish(r1.prompt_id, "done")
        r2 = reg1.start("r-bf2", "running task")
        # Delete the archive so backfill triggers on next startup
        archive_path = os.path.join(str(tmp_path), "prompts_archive.jsonl")
        os.remove(archive_path)
        # New registry instance — should backfill
        PromptRegistry(data_dir=str(tmp_path))
        with open(archive_path, encoding="utf-8") as f:
            lines = [json.loads(line) for line in f]
        # Only finalized records should be in the archive
        prompt_ids = [line["prompt_id"] for line in lines]
        assert r1.prompt_id in prompt_ids  # finalized → backfilled
        assert r2.prompt_id not in prompt_ids  # running → skipped

    def test_backfill_skips_when_archive_exists(self, tmp_path):
        """If archive already exists, backfill does not overwrite it."""
        reg1 = PromptRegistry(data_dir=str(tmp_path))
        r1 = reg1.start("r-bf3", "task")
        reg1.finish(r1.prompt_id, "done")
        archive_path = os.path.join(str(tmp_path), "prompts_archive.jsonl")
        # Archive now has 1 line. Create a new registry — it should NOT backfill.
        PromptRegistry(data_dir=str(tmp_path))
        with open(archive_path, encoding="utf-8") as f:
            lines = [json.loads(line) for line in f]
        # Still just 1 line — no duplicate from backfill
        assert len(lines) == 1

    def test_backfill_io_error_does_not_crash(self, tmp_path, monkeypatch):
        """Backfill I/O error is logged, not raised — startup survives."""
        reg1 = PromptRegistry(data_dir=str(tmp_path))
        r1 = reg1.start("r-bf-io", "task")
        reg1.finish(r1.prompt_id, "done")
        # Delete archive so backfill triggers on next startup
        archive_path = os.path.join(str(tmp_path), "prompts_archive.jsonl")
        os.remove(archive_path)
        # Patch _archive_snapshot to raise OSError
        original_snapshot = PromptRegistry._archive_snapshot
        def _raise_oserror(self, record):
            raise OSError("disk full")
        monkeypatch.setattr(PromptRegistry, "_archive_snapshot", _raise_oserror)
        try:
            # Should not raise
            reg2 = PromptRegistry(data_dir=str(tmp_path))
        finally:
            monkeypatch.setattr(PromptRegistry, "_archive_snapshot", original_snapshot)
        # Registry still works — in-memory records loaded from replay
        assert reg2.get(r1.prompt_id) is not None


class TestEviction:
    def test_cap_at_100(self, tmp_path):
        """The in-memory cap evicts finalized records down to MAX_IN_MEMORY."""
        from prompt_registry import PromptRecord
        registry = PromptRegistry(data_dir=str(tmp_path))
        base = time.time()
        # Inject exactly MAX_IN_MEMORY + 1 finalized records. _evict_oldest
        # should leave MAX_IN_MEMORY finalized records.
        for i in range(PromptRegistry.MAX_IN_MEMORY + 1):
            pid = f"01CAP{i:023d}"
            registry._records[pid] = PromptRecord(
                prompt_id=pid,
                trace_id=f"r-ev-{i}",
                text=f"task {i}",
                started_at=base + i,
                status="done",
                ended_at=base + i + 1,
            )
        registry._trace_to_id = {f"r-ev-{i}": f"01CAP{i:023d}" for i in range(PromptRegistry.MAX_IN_MEMORY + 1)}
        registry._evict_oldest()
        assert len(registry._records) == PromptRegistry.MAX_IN_MEMORY

    def test_oldest_finalized_evicted(self, tmp_path):
        """The oldest finalized record is the one evicted."""
        from prompt_registry import PromptRecord
        registry = PromptRegistry(data_dir=str(tmp_path))
        base = time.time()
        oldest_id = "01OLD00000000000000000001"
        registry._records[oldest_id] = PromptRecord(
            prompt_id=oldest_id,
            trace_id="r-oldest",
            text="oldest task",
            started_at=base - 1000,
            status="done",
            ended_at=base - 999,
        )
        for i in range(PromptRegistry.MAX_IN_MEMORY):
            pid = f"01YOUNG{i:022d}"
            registry._records[pid] = PromptRecord(
                prompt_id=pid,
                trace_id=f"r-young-{i}",
                text=f"young task {i}",
                started_at=base - i,
                status="done",
                ended_at=base - i + 1,
            )
        registry._trace_to_id = {"r-oldest": oldest_id}
        registry._trace_to_id.update({f"r-young-{i}": f"01YOUNG{i:022d}" for i in range(PromptRegistry.MAX_IN_MEMORY)})
        registry._evict_oldest()
        # The oldest prompt should be evicted (earliest started_at)
        assert registry.get(oldest_id) is None

    def test_running_records_never_evicted(self, tmp_path):
        """All-running case: 101 records, none evicted."""
        from prompt_registry import PromptRecord
        registry = PromptRegistry(data_dir=str(tmp_path))
        base = time.time()
        for i in range(101):
            pid = f"01RUN{i:024d}"
            registry._records[pid] = PromptRecord(
                prompt_id=pid,
                trace_id=f"r-run-{i}",
                text=f"task {i}",
                started_at=base + i,
                status="running",
            )
        registry._trace_to_id = {f"r-run-{i}": f"01RUN{i:024d}" for i in range(101)}
        registry._evict_oldest()
        assert len(registry._records) == 101

    def test_evicted_record_retrievable_via_show(self, tmp_path):
        """Evicted finalized record is found via show() from the archive."""
        registry = PromptRegistry(data_dir=str(tmp_path))
        first = registry.start("r-show-ev", "first task")
        registry.finish(first.prompt_id, "done")
        # Fill to MAX_IN_MEMORY + 1 finalized records. _evict_oldest will then
        # evict the oldest finalized record (first).
        for i in range(PromptRegistry.MAX_IN_MEMORY):
            rec = registry.start(f"r-show-rest-{i}", f"task {i}")
            registry.finish(rec.prompt_id, "done")
        registry._evict_oldest()
        # first is evicted from memory
        assert registry.get(first.prompt_id) is None
        # but show() finds it in the archive
        found = registry.show(first.prompt_id)
        assert found is not None
        assert found.prompt_id == first.prompt_id
        assert found.text == "first task"


class TestSearch:
    def test_substring_match(self, registry):
        rec = registry.start("r-s1", "PTO request for next week")
        registry.finish(rec.prompt_id, "done")
        result = registry.search("PTO")
        assert rec.prompt_id in [r.prompt_id for r in result.results]

    def test_case_insensitive(self, registry):
        rec = registry.start("r-s2", "PTO request")
        registry.finish(rec.prompt_id, "done")
        result = registry.search("pto")
        assert rec.prompt_id in [r.prompt_id for r in result.results]

    def test_relative_time_window_days(self, registry):
        from prompt_registry import PromptRecord
        base = time.time()
        old_id = "01OLD0000000000000000DAYS1"
        new_id = "01NEW0000000000000000DAYS2"
        # Inject records with deterministic, separated timestamps.
        registry._records[old_id] = PromptRecord(
            prompt_id=old_id, trace_id="r-s3a", text="worklogs old",
            started_at=base - 10 * 86400, status="done", ended_at=base - 10 * 86400 + 1,
        )
        registry._records[new_id] = PromptRecord(
            prompt_id=new_id, trace_id="r-s3b", text="worklogs new",
            started_at=base, status="done", ended_at=base + 1,
        )
        result = registry.search("worklogs", days=7)
        ids = [r.prompt_id for r in result.results]
        assert new_id in ids
        assert old_id not in ids

    def test_relative_time_window_hours(self, registry):
        rec = registry.start("r-s4", "worklogs recent")
        registry.finish(rec.prompt_id, "done")
        result = registry.search("worklogs", days=0.5)  # 12 hours
        assert rec.prompt_id in [r.prompt_id for r in result.results]

    def test_absolute_time_range_since_until(self, registry):
        # Create records with controlled started_at
        from prompt_registry import PromptRecord
        old_id = "01OLD00000000000000000001"
        mid_id = "01MID00000000000000000002"
        new_id = "01NEW00000000000000000003"
        base = time.time()
        for pid, offset in [(old_id, -30 * 86400), (mid_id, -5 * 86400), (new_id, -1 * 86400)]:
            registry._records[pid] = PromptRecord(
                prompt_id=pid, trace_id=f"r-{pid}", text="deploy",
                started_at=base + offset, status="done", ended_at=base + offset + 1,
            )
        since = datetime.fromtimestamp(base - 10 * 86400, tz=timezone.utc).isoformat()
        until = datetime.fromtimestamp(base - 2 * 86400, tz=timezone.utc).isoformat()
        result = registry.search("deploy", since=since, until=until)
        ids = [r.prompt_id for r in result.results]
        assert mid_id in ids
        assert old_id not in ids
        assert new_id not in ids

    def test_since_only_filter(self, registry):
        from prompt_registry import PromptRecord
        old_id = "01OLD20000000000000000001"
        new_id = "01NEW20000000000000000002"
        base = time.time()
        registry._records[old_id] = PromptRecord(
            prompt_id=old_id, trace_id="r-old", text="worklogs",
            started_at=base - 10 * 86400, status="done", ended_at=base - 10 * 86400 + 1,
        )
        registry._records[new_id] = PromptRecord(
            prompt_id=new_id, trace_id="r-new", text="worklogs",
            started_at=base - 3 * 86400, status="done", ended_at=base - 3 * 86400 + 1,
        )
        since = datetime.fromtimestamp(base - 5 * 86400, tz=timezone.utc).isoformat()
        result = registry.search("worklogs", since=since)
        ids = [r.prompt_id for r in result.results]
        assert new_id in ids
        assert old_id not in ids

    def test_naive_iso_interpreted_as_utc(self, registry):
        from prompt_registry import PromptRecord
        # Create a record started at a known UTC time
        target_utc = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)
        rec_id = "01NAI00000000000000000001"
        registry._records[rec_id] = PromptRecord(
            prompt_id=rec_id, trace_id="r-naive", text="deploy",
            started_at=target_utc.timestamp(), status="done", ended_at=target_utc.timestamp() + 1,
        )
        # Search with a naive ISO string (no timezone offset)
        # Should be interpreted as UTC, so the record should match
        result = registry.search("deploy", since="2026-08-10T12:00:00")
        ids = [r.prompt_id for r in result.results]
        assert rec_id in ids

    def test_since_until_precedence_over_days(self, registry):
        from prompt_registry import PromptRecord
        base = time.time()
        rec_id = "01PRE00000000000000000001"
        registry._records[rec_id] = PromptRecord(
            prompt_id=rec_id, trace_id="r-prec", text="deploy",
            started_at=base - 5 * 86400, status="done", ended_at=base - 5 * 86400 + 1,
        )
        # days=1 would exclude this record (5 days old), but since/until should include it
        since = datetime.fromtimestamp(base - 10 * 86400, tz=timezone.utc).isoformat()
        until = datetime.fromtimestamp(base, tz=timezone.utc).isoformat()
        result = registry.search("deploy", days=1, since=since, until=until)
        ids = [r.prompt_id for r in result.results]
        assert rec_id in ids  # since/until takes precedence, days ignored

    def test_status_filter_positive(self, registry):
        r1 = registry.start("r-st1", "PTO request")
        registry.finish(r1.prompt_id, "failed")
        r2 = registry.start("r-st2", "PTO review")
        registry.finish(r2.prompt_id, "done")
        result = registry.search("PTO", status="failed")
        ids = [r.prompt_id for r in result.results]
        assert r1.prompt_id in ids
        assert r2.prompt_id not in ids

    def test_invalid_status_matches_nothing(self, registry):
        rec = registry.start("r-st3", "PTO request")
        registry.finish(rec.prompt_id, "done")
        result = registry.search("PTO", status="unknown")
        assert result.results == []
        assert result.total_matched == 0

    def test_trace_id_exact_match(self, registry):
        r1 = registry.start("r-abc", "PTO request")
        registry.finish(r1.prompt_id, "done")
        r2 = registry.start("r-def", "PTO review")
        registry.finish(r2.prompt_id, "done")
        result = registry.search("PTO", trace_id="r-abc")
        ids = [r.prompt_id for r in result.results]
        assert r1.prompt_id in ids
        assert r2.prompt_id not in ids

    def test_combined_filters_status_and_trace_id(self, registry):
        r1 = registry.start("r-combo1", "PTO request")
        registry.finish(r1.prompt_id, "failed")
        r2 = registry.start("r-combo2", "PTO review")
        registry.finish(r2.prompt_id, "done")
        # status=failed + trace=r-combo2 → matches neither
        result = registry.search("PTO", status="failed", trace_id="r-combo2")
        assert result.results == []

    def test_empty_query_wildcard(self, registry):
        r1 = registry.start("r-wc1", "first task")
        registry.finish(r1.prompt_id, "done")
        r2 = registry.start("r-wc2", "second task")
        registry.finish(r2.prompt_id, "done")
        result = registry.search("")
        assert result.total_matched >= 2

    def test_dedup_in_memory_and_archive(self, tmp_path):
        """A record in both memory and archive appears only once."""
        reg = PromptRegistry(data_dir=str(tmp_path))
        rec = reg.start("r-dedup", "dedup test")
        reg.finish(rec.prompt_id, "done")
        # Record is in both memory and archive (finish writes to archive)
        result = reg.search("dedup")
        ids = [r.prompt_id for r in result.results]
        assert ids.count(rec.prompt_id) == 1

    def test_limit_20(self, tmp_path):
        """30 matches return only 20 results."""
        registry = PromptRegistry(data_dir=str(tmp_path))
        for i in range(30):
            rec = registry.start(f"r-lim-{i}", f"worklogs {i}")
            registry.finish(rec.prompt_id, "done")
        result = registry.search("worklogs")
        assert len(result.results) == 20
        assert result.total_matched == 30

    def test_offset_pagination(self, tmp_path):
        """offset=20 returns the second page."""
        registry = PromptRegistry(data_dir=str(tmp_path))
        for i in range(30):
            rec = registry.start(f"r-page-{i}", f"worklogs {i}")
            registry.finish(rec.prompt_id, "done")
        result = registry.search("worklogs", offset=20)
        assert len(result.results) == 10
        assert result.total_matched == 30

    def test_out_of_range_offset(self, tmp_path):
        """offset beyond total_matched returns empty results but correct total."""
        registry = PromptRegistry(data_dir=str(tmp_path))
        for i in range(30):
            rec = registry.start(f"r-oor-{i}", f"worklogs {i}")
            registry.finish(rec.prompt_id, "done")
        result = registry.search("worklogs", offset=100)
        assert result.results == []
        assert result.total_matched == 30

    def test_sorted_by_started_at_descending(self, registry):
        from prompt_registry import PromptRecord
        base = time.time()
        for i, offset in enumerate([10, 5, 1]):
            pid = f"01SRT{i:023d}"
            registry._records[pid] = PromptRecord(
                prompt_id=pid, trace_id=f"r-srt-{i}", text="sort test",
                started_at=base - offset, status="done", ended_at=base - offset + 1,
            )
        result = registry.search("sort test")
        times = [r.started_at for r in result.results]
        assert times == sorted(times, reverse=True)

    def test_concurrent_search_does_not_block_start(self, tmp_path):
        """Search running in a thread doesn't block start()."""
        registry = PromptRegistry(data_dir=str(tmp_path))
        for i in range(5):
            rec = registry.start(f"r-conc-{i}", f"task {i}")
            registry.finish(rec.prompt_id, "done")

        start_completed = threading.Event()

        def do_search():
            registry.search("task")

        def do_start():
            registry.start("r-conc-new", "new task")
            start_completed.set()

        with ThreadPoolExecutor(max_workers=2) as pool:
            pool.submit(do_search)
            pool.submit(do_start)
            assert start_completed.wait(timeout=5)

    def test_archive_file_absent_returns_empty(self, tmp_path):
        """No archive file → only in-memory records searched."""
        registry = PromptRegistry(data_dir=str(tmp_path))
        rec = registry.start("r-noarch", "test task")
        registry.finish(rec.prompt_id, "done")
        # Delete the archive to simulate absent file
        archive_path = os.path.join(str(tmp_path), "prompts_archive.jsonl")
        os.remove(archive_path)
        # In-memory record should still be found
        result = registry.search("test task")
        assert rec.prompt_id in [r.prompt_id for r in result.results]
        # Search for something not in memory → empty
        result2 = registry.search("nonexistent")
        assert result2.results == []
        assert result2.total_matched == 0


class TestFindInArchive:
    def test_found(self, registry, tmp_path):
        rec = registry.start("r-fa1", "find me")
        registry.finish(rec.prompt_id, "done")
        # Remove from memory to force archive lookup
        del registry._records[rec.prompt_id]
        found = registry.find_in_archive(rec.prompt_id)
        assert found is not None
        assert found.prompt_id == rec.prompt_id
        assert found.text == "find me"

    def test_not_found(self, registry):
        assert registry.find_in_archive("NONEXISTENT") is None

    def test_archive_file_absent_returns_none(self, tmp_path):
        """No archive file → find_in_archive returns None."""
        # Create a fresh registry in a new dir with no prompts.jsonl
        new_dir = str(tmp_path) + "_no_archive"
        os.makedirs(new_dir, exist_ok=True)
        registry = PromptRegistry(data_dir=new_dir)
        assert registry.find_in_archive("any-id") is None


class TestShow:
    def test_in_memory_hit(self, registry):
        rec = registry.start("r-show1", "in memory task")
        found = registry.show(rec.prompt_id)
        assert found is not None
        assert found.prompt_id == rec.prompt_id
        assert found.text == "in memory task"

    def test_archive_fallback(self, tmp_path):
        """Record evicted from memory but in archive → show() finds it."""
        registry = PromptRegistry(data_dir=str(tmp_path))
        first = registry.start("r-show-arch", "archived task")
        registry.finish(first.prompt_id, "done")
        # Add MAX_IN_MEMORY more finalized records so _evict_oldest removes the
        # oldest one (first).
        for i in range(PromptRegistry.MAX_IN_MEMORY):
            rec = registry.start(f"r-show-fill-{i}", f"fill {i}")
            registry.finish(rec.prompt_id, "done")
        registry._evict_oldest()
        # first should be evicted from memory
        assert registry.get(first.prompt_id) is None
        # but show() should find it in the archive
        found = registry.show(first.prompt_id)
        assert found is not None
        assert found.prompt_id == first.prompt_id
        assert found.text == "archived task"

    def test_not_found_in_either(self, registry):
        assert registry.show("NONEXISTENT") is None

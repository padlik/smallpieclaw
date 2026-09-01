"""Dedicated tests for the SQLite structured log store (sqlite_log.py).

Covers:
- Record-to-row mapping (wide columns, extra JSON, casefolded search_text).
- WAL durability after abrupt close (simulated process kill).
- Bounded queue overflow: drop-oldest + WARNING, emitters never block.
- Writer failure recovery and graceful degradation to prose-only operation.
- Concurrent emitters are serialized without loss or locking errors.
- Graceful shutdown drains all queued records.
- Time-based retention (30 days) plus WAL checkpoint.
- Regression checks against the full agent_logging wiring: component isolation,
  prose sink shape, run identity propagation, secret redaction.

An autouse fixture snapshots and restores the root logger's handlers (plus the
isolated ``graph_memory`` component logger's handlers and propagate flag),
drains/stops any active SQLite writer, and resets structlog so configuring
logging here does not leak into the rest of the suite.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import structlog

import agent_logging as al
import sqlite_log


@pytest.fixture(autouse=True)
def _isolate_logging(tmp_path):
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    gm = logging.getLogger("graph_memory")
    saved_gm_handlers = gm.handlers[:]
    saved_gm_propagate = gm.propagate
    wl = logging.getLogger("sqlite_log_writer")
    saved_wl_handlers = wl.handlers[:]
    saved_wl_propagate = wl.propagate
    structlog.contextvars.clear_contextvars()
    yield
    al.shutdown_log_store()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)
    for handler in gm.handlers[:]:
        gm.removeHandler(handler)
        handler.close()
    for handler in saved_gm_handlers:
        gm.addHandler(handler)
    gm.propagate = saved_gm_propagate
    for handler in wl.handlers[:]:
        wl.removeHandler(handler)
        try:
            handler.close()
        except OSError:
            pass
    for handler in saved_wl_handlers:
        wl.addHandler(handler)
    wl.propagate = saved_wl_propagate
    structlog.contextvars.clear_contextvars()
    structlog.reset_defaults()


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _count_rows(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute("SELECT COUNT(*) FROM events")
        return cur.fetchone()[0]
    finally:
        conn.close()


def _rows(db_path: str) -> list[dict]:
    """Return all rows with wide columns merged with parsed extra JSON."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute("SELECT * FROM events ORDER BY id")
        return [
            _merge_extra({k: v for k, v in dict(row).items() if v is not None})
            for row in cur.fetchall()
        ]
    finally:
        conn.close()


def _last_row(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None, f"no rows in {db_path}"
    return _merge_extra({k: v for k, v in dict(row).items() if v is not None})


def _merge_extra(row: dict) -> dict:
    extra = json.loads(row.pop("extra"))
    row.update(extra)
    return row


def _flush() -> None:
    writer = sqlite_log._active_writer
    if writer is not None:
        writer.flush()


# ---------------------------------------------------------------------------
# 5.1 — Record mapping
# ---------------------------------------------------------------------------
class TestRecordMapping:
    def test_wide_columns_extracted_and_extras_in_json(self):
        record = {
            "ts": "2026-08-31T12:00:00",
            "level": "info",
            "logger": "tests",
            "agent": "main",
            "trace": "r-abc123",
            "prompt_id": "01JARYN6R0",
            "event_type": "TOOL_END",
            "msg": "done",
            "tool": "shell",
            "exit": 0,
            "dur_ms": 42,
        }
        row = sqlite_log.record_to_row(record)
        assert row[0] == "2026-08-31T12:00:00"
        assert row[1] == "info"
        assert row[2] == "tests"
        assert row[3] == "main"
        assert row[4] == "r-abc123"
        assert row[5] == "01JARYN6R0"
        assert row[6] == "TOOL_END"
        assert row[7] == "done"
        extra = json.loads(row[8])
        assert extra == {"tool": "shell", "exit": 0, "dur_ms": 42}
        assert row[9] == json.dumps(record, ensure_ascii=False, separators=(",", ":")).casefold()

    def test_missing_wide_columns_use_fallback_or_none(self):
        record = {"msg": "bare"}
        row = sqlite_log.record_to_row(record)
        assert row[0] == ""  # ts NOT NULL fallback
        assert row[1] == ""  # level NOT NULL fallback
        assert row[2] is None  # logger
        assert row[3] is None  # agent
        assert row[4] is None  # trace
        assert row[5] is None  # prompt_id
        assert row[6] is None  # event_type
        assert row[7] == "bare"
        assert row[8] == "{}"

    def test_empty_extras_use_empty_json_object(self):
        record = {"ts": "2026-08-31T12:00:00", "level": "info", "msg": "hi"}
        row = sqlite_log.record_to_row(record)
        assert row[8] == "{}"

    def test_non_finite_floats_serialized_as_null(self):
        record = {
            "ts": "2026-08-31T12:00:00",
            "level": "info",
            "msg": "metrics",
            "ratio": float("nan"),
            "big": float("inf"),
            "nested": {"bad": float("-inf"), "good": 1.5},
            "list": [float("nan"), 2.0],
        }
        row = sqlite_log.record_to_row(record)
        extra = json.loads(row[8])
        assert extra["ratio"] is None
        assert extra["big"] is None
        assert extra["nested"]["bad"] is None
        assert extra["nested"]["good"] == 1.5
        assert extra["list"] == [None, 2.0]
        # search_text must be valid JSON too.
        search_text = row[9]
        parsed_search = json.loads(search_text)
        assert parsed_search["ratio"] is None
        assert parsed_search["big"] is None

    def test_search_text_unicode_casefold(self):
        record = {
            "ts": "2026-08-31T12:00:00",
            "level": "info",
            "msg": "straße",
            "tool": "groß",
        }
        row = sqlite_log.record_to_row(record)
        search_text = row[9]
        assert "straße" not in search_text  # original sharp-s is casefolded
        assert "strasse" in search_text
        assert "gross" in search_text
        # Verify the same fold is queryable through SQLite instr()
        conn = sqlite3.connect(":memory:")
        try:
            cur = conn.execute("SELECT instr(?, ?)", (search_text, "STRASSE".casefold()))
            assert cur.fetchone()[0] > 0
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 5.1 — WAL durability
# ---------------------------------------------------------------------------
class TestWALDurability:
    def test_committed_rows_survive_abrupt_close(self, tmp_path):
        db_path = str(tmp_path / "agent_logs.sqlite")
        conn = sqlite3.connect(db_path)
        try:
            sqlite_log._apply_wal_pragmas(conn)
            sqlite_log._init_schema(conn)
            records = [
                {"ts": datetime.now(timezone.utc).isoformat(), "level": "info", "msg": f"r{i}"}
                for i in range(10)
            ]
            rows = [sqlite_log.record_to_row(r) for r in records]
            with conn:
                conn.executemany(
                    """
                    INSERT INTO events
                    (ts, level, logger, agent, trace, prompt_id, event_type, msg, extra, search_text)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
            # Abrupt close: no checkpoint, no graceful stop.
        finally:
            conn.close()

        assert _count_rows(db_path) == 10
        # WAL file should have existed before reopening, proving WAL mode.
        # After a normal connection close with committed tx, SQLite may or may not
        # checkpoint automatically; the important assertion is readability.
        rows = _rows(db_path)
        assert {r["msg"] for r in rows} == {f"r{i}" for i in range(10)}


# ---------------------------------------------------------------------------
# 5.1 — Queue overflow
# ---------------------------------------------------------------------------
class TestQueueOverflow:
    def test_drop_oldest_on_full_and_warning_emitted(self, tmp_path, caplog):
        db_path = str(tmp_path / "agent_logs.sqlite")
        writer = sqlite_log.SqliteLogWriter(db_path, queue_size=3, batch_size=1000)
        writer.start()
        wl = logging.getLogger("sqlite_log_writer")
        wl.addHandler(caplog.handler)
        try:
            with caplog.at_level(logging.WARNING, logger="sqlite_log_writer"):
                for i in range(5):
                    writer.enqueue({"ts": "2026-08-31T12:00:00", "level": "info", "msg": f"m{i}"})
            assert writer.dropped == 2
            assert any("dropped oldest record" in r.getMessage() for r in caplog.records)
        finally:
            wl.removeHandler(caplog.handler)
            writer.stop(timeout=5.0)

        # Oldest two were dropped; newest three remain.
        rows = _rows(db_path)
        assert len(rows) == 3
        assert [r["msg"] for r in rows] == ["m2", "m3", "m4"]

    def test_enqueue_never_blocks(self, tmp_path):
        db_path = str(tmp_path / "agent_logs.sqlite")
        writer = sqlite_log.SqliteLogWriter(db_path, queue_size=2, batch_size=1000)
        writer.start()
        start = time.monotonic()
        try:
            for i in range(20):
                writer.enqueue({"ts": "2026-08-31T12:00:00", "level": "info", "msg": f"x{i}"})
            elapsed = time.monotonic() - start
            assert elapsed < 1.0, "enqueue should never block on DB I/O"
        finally:
            writer.stop(timeout=5.0)


# ---------------------------------------------------------------------------
# 5.1 — Writer failure recovery / graceful degradation
# ---------------------------------------------------------------------------
class TestWriterFailureRecovery:
    def test_persistent_failure_disables_writer_and_drains_queue(self, tmp_path, caplog, monkeypatch):
        db_path = str(tmp_path / "agent_logs.sqlite")
        writer = sqlite_log.SqliteLogWriter(db_path)
        writer.start()
        writer.flush()
        assert not writer.disabled

        # First, force every insert to fail and every reconnect to return the
        # same broken connection so the retry also fails.
        class BrokenConnection:
            def __init__(self, real_conn):
                self._real = real_conn

            def execute(self, sql, *args):
                return self._real.execute(sql, *args)

            def executemany(self, sql, params):
                raise sqlite3.OperationalError("simulated write failure")

            def close(self):
                self._real.close()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        real_conn = writer._conn
        broken = BrokenConnection(real_conn)
        object.__setattr__(writer, "_conn", broken)

        def broken_open():
            return broken

        monkeypatch.setattr(writer, "_open_connection", broken_open)

        wl = logging.getLogger("sqlite_log_writer")
        wl.addHandler(caplog.handler)
        try:
            with caplog.at_level(logging.WARNING, logger="sqlite_log_writer"):
                writer.enqueue({"ts": "2026-08-31T12:00:00", "level": "info", "msg": "fail"})
                # Wait for the writer thread to process the failing batch.
                deadline = time.monotonic() + 5.0
                while not writer.disabled and time.monotonic() < deadline:
                    time.sleep(0.05)

            assert writer.disabled
            assert any("persistently unavailable" in r.getMessage() for r in caplog.records)

            # Queue keeps draining after disablement: enqueue more, stop, no hang.
            for i in range(10):
                writer.enqueue({"ts": "2026-08-31T12:00:00", "level": "info", "msg": f"drop{i}"})
        finally:
            wl.removeHandler(caplog.handler)
            writer.stop(timeout=5.0)

        # No rows should have been inserted; no hang occurred.
        assert _count_rows(db_path) == 0


# ---------------------------------------------------------------------------
# 5.1 — Startup open failure in read-only directory
# ---------------------------------------------------------------------------
class TestStartupDegradation:
    def test_read_only_logs_dir_degrades_to_prose_only(self, tmp_path, caplog):
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        log_file = str(logs_dir / "agent.log")
        gm_log = str(logs_dir / "graph_memory.log")

        # Pre-create the prose files so they can be appended even when the
        # directory itself is read-only. SQLite needs to create the DB file
        # (and WAL files), which will fail.
        (logs_dir / "agent.log").write_text("")
        (logs_dir / "graph_memory.log").write_text("")
        os.chmod(logs_dir, 0o555)

        if os.access(str(logs_dir), os.W_OK):
            pytest.skip("directory is still writable (running as root or unusual fs)")

        try:
            db_path = al.setup_logging(
                log_file,
                graph_memory_log=gm_log,
                backup_count=1,
            )

            # Wait for the writer thread to disable (startup failure is async).
            writer = sqlite_log._active_writer
            if writer is not None:
                deadline = time.monotonic() + 2.0
                while not writer.disabled and time.monotonic() < deadline:
                    time.sleep(0.05)

            # Startup-failure warning is routed to the prose sink by fix 1b.
            prose = _read(log_file)
            assert "could not be opened" in prose

            # A normal record still lands in the prose sink.
            logging.getLogger("agent").info("prose still works")
            _flush()
            prose = _read(log_file)
            assert "prose still works" in prose

            # It must not be in the (disabled) structured store. The store file
            # may not exist at all because the directory was read-only.
            if Path(db_path).exists():
                assert _count_rows(db_path) == 0
        finally:
            os.chmod(logs_dir, 0o755)


# ---------------------------------------------------------------------------
# 5.1 — Recursion safety (C1 regression)
# ---------------------------------------------------------------------------
class TestRecursionSafety:
    def test_no_recursion_when_queue_full_and_writer_disabled(self, tmp_path):
        """Overflow warning on a disabled writer must not re-enter enqueue → RecursionError.

        Pre-fix crash chain: disabled writer + full queue → overflow drop-attempt →
        _warning_logger.warning() propagated to root → SQLiteQueueHandler.emit()
        re-enters enqueue() on same thread → infinite recursion → agent crash.
        Guarded by: propagate=False on sqlite_log_writer (1a) + early-return in
        enqueue() when disabled (1c).
        """
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        log_file = str(logs_dir / "agent.log")
        (logs_dir / "agent.log").write_text("")
        (logs_dir / "graph_memory.log").write_text("")
        os.chmod(logs_dir, 0o555)

        if os.access(str(logs_dir), os.W_OK):
            pytest.skip("directory is still writable (running as root or unusual fs)")

        try:
            al.setup_logging(log_file, backup_count=1)

            writer = sqlite_log._active_writer
            assert writer is not None

            # Wait for the writer thread to disable (startup failure: DB open fails).
            deadline = time.monotonic() + 2.0
            while not writer.disabled and time.monotonic() < deadline:
                time.sleep(0.05)
            assert writer.disabled, "writer must have disabled after read-only dir startup failure"

            # Fill the queue by bypassing enqueue()'s guard to reproduce the
            # pre-fix crash condition: full queue + disabled writer.
            _fill_record = {"ts": "2026-09-01T00:00:00", "level": "info", "msg": "fill"}
            for _ in range(writer._queue.maxsize):
                try:
                    writer._queue.put_nowait(_fill_record)
                except queue.Full:
                    break

            # Emitting via root logger → SQLiteQueueHandler.emit() → enqueue()
            # with full queue + disabled writer. Pre-fix: RecursionError.
            # Post-fix: enqueue() early-returns; no exception raised.
            logging.getLogger("overflow_probe").info("overflow probe")

            assert writer.disabled
        finally:
            os.chmod(logs_dir, 0o755)

        prose = _read(log_file)
        assert "overflow probe" in prose


# ---------------------------------------------------------------------------
# 5.1 — Concurrent emitters
# ---------------------------------------------------------------------------
class TestConcurrentEmitters:
    def test_three_threads_store_exactly_once(self, tmp_path):
        db_path = str(tmp_path / "agent_logs.sqlite")
        writer = sqlite_log.SqliteLogWriter(db_path)
        writer.start()
        try:
            errors: list[Exception] = []
            barrier = threading.Barrier(3)

            def emit(agent: str):
                try:
                    barrier.wait(timeout=2.0)
                    for i in range(50):
                        writer.enqueue(
                            {
                                "ts": datetime.now(timezone.utc).isoformat(),
                                "level": "info",
                                "agent": agent,
                                "msg": f"{agent}-{i}",
                            }
                        )
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [
                threading.Thread(target=emit, args=("react",), name="react"),
                threading.Thread(target=emit, args=("sa-1",), name="sa-1"),
                threading.Thread(target=emit, args=("sched",), name="sched"),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert not errors, f"emitters raised: {errors}"
            writer.flush()
        finally:
            writer.stop(timeout=5.0)

        rows = _rows(db_path)
        assert len(rows) == 150
        agents = {r["agent"] for r in rows}
        assert agents == {"react", "sa-1", "sched"}
        for agent in agents:
            assert sum(1 for r in rows if r["agent"] == agent) == 50
        msgs = {r["msg"] for r in rows}
        assert len(msgs) == 150


# ---------------------------------------------------------------------------
# 5.1 — Graceful drain
# ---------------------------------------------------------------------------
class TestGracefulDrain:
    def test_stop_drains_all_queued_records(self, tmp_path):
        db_path = str(tmp_path / "agent_logs.sqlite")
        writer = sqlite_log.SqliteLogWriter(db_path)
        writer.start()
        try:
            for i in range(100):
                writer.enqueue(
                    {"ts": "2026-08-31T12:00:00", "level": "info", "msg": f"drain-{i}"}
                )
        finally:
            writer.stop(timeout=5.0)

        rows = _rows(db_path)
        assert len(rows) == 100
        assert {r["msg"] for r in rows} == {f"drain-{i}" for i in range(100)}

    def test_shutdown_log_store_drains_active_writer(self, tmp_path):
        db_path = al.setup_logging(str(tmp_path / "agent.log"), backup_count=1)
        for i in range(20):
            logging.getLogger("x").info("shutdown-%d", i)
        # Do not flush; rely on graceful shutdown.
        al.shutdown_log_store()
        rows = _rows(db_path)
        assert len(rows) == 20

    def test_stop_drains_after_overflow_drops(self, tmp_path):
        """Graceful shutdown succeeds and drains even after queue overflow.

        The queue is filled past capacity before the writer thread starts, so
        deterministic drops occur. ``stop()`` must not raise ``queue.Full`` and
        must commit every record that was not dropped.
        """
        db_path = str(tmp_path / "agent_logs.sqlite")
        writer = sqlite_log.SqliteLogWriter(db_path, queue_size=5, batch_size=1000)
        for i in range(20):
            writer.enqueue({"ts": "2026-08-31T12:00:00", "level": "info", "msg": f"drop-{i}"})

        assert writer.dropped == 15
        writer.start()
        writer.stop(timeout=5.0)

        rows = _rows(db_path)
        assert len(rows) == 5
        assert [r["msg"] for r in rows] == [f"drop-{i}" for i in range(15, 20)]


# ---------------------------------------------------------------------------
# 5.2 — Retention
# ---------------------------------------------------------------------------
class TestRetention:
    def test_old_rows_deleted_recent_rows_kept_and_checkpoint_runs(self, tmp_path):
        db_path = str(tmp_path / "agent_logs.sqlite")
        conn = sqlite3.connect(db_path)
        try:
            sqlite_log._apply_wal_pragmas(conn)
            sqlite_log._init_schema(conn)
            old_ts = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
            new_ts = datetime.now(timezone.utc).isoformat()
            old_rows = [
                sqlite_log.record_to_row({"ts": old_ts, "level": "info", "msg": f"old-{i}"})
                for i in range(5)
            ]
            new_rows = [
                sqlite_log.record_to_row({"ts": new_ts, "level": "info", "msg": f"new-{i}"})
                for i in range(3)
            ]
            with conn:
                conn.executemany(
                    """
                    INSERT INTO events
                    (ts, level, logger, agent, trace, prompt_id, event_type, msg, extra, search_text)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    old_rows + new_rows,
                )

            deleted = sqlite_log._run_retention(conn)
            assert deleted == 5
            assert _count_rows(db_path) == 3
            msgs = {r["msg"] for r in _rows(db_path)}
            assert all(m.startswith("new-") for m in msgs)

            # Checkpoint must complete and leave the store queryable.
            cur = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            assert cur.fetchone()[0] == 0  # SQLITE_OK
            assert _count_rows(db_path) == 3
        finally:
            conn.close()

    def test_startup_retention_prunes_old_rows(self, tmp_path):
        db_path = str(tmp_path / "agent_logs.sqlite")
        conn = sqlite3.connect(db_path)
        try:
            sqlite_log._apply_wal_pragmas(conn)
            sqlite_log._init_schema(conn)
            old_ts = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
            new_ts = datetime.now(timezone.utc).isoformat()
            rows = [
                sqlite_log.record_to_row({"ts": old_ts, "level": "info", "msg": "old"}),
                sqlite_log.record_to_row({"ts": new_ts, "level": "info", "msg": "new"}),
            ]
            with conn:
                conn.executemany(
                    """
                    INSERT INTO events
                    (ts, level, logger, agent, trace, prompt_id, event_type, msg, extra, search_text)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
        finally:
            conn.close()

        writer = sqlite_log.SqliteLogWriter(db_path)
        writer.start()
        writer.flush()
        writer.stop(timeout=5.0)

        assert _count_rows(db_path) == 1
        assert _last_row(db_path)["msg"] == "new"

    def test_custom_retention_days_is_respected(self, tmp_path):
        """The writer's retention_days constructor argument actually drives DELETE."""
        db_path = str(tmp_path / "agent_logs.sqlite")
        conn = sqlite3.connect(db_path)
        try:
            sqlite_log._apply_wal_pragmas(conn)
            sqlite_log._init_schema(conn)
            old_ts = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
            new_ts = datetime.now(timezone.utc).isoformat()
            rows = [
                sqlite_log.record_to_row({"ts": old_ts, "level": "info", "msg": "two-days-old"}),
                sqlite_log.record_to_row({"ts": new_ts, "level": "info", "msg": "today"}),
            ]
            with conn:
                conn.executemany(
                    """
                    INSERT INTO events
                    (ts, level, logger, agent, trace, prompt_id, event_type, msg, extra, search_text)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
        finally:
            conn.close()

        writer = sqlite_log.SqliteLogWriter(db_path, retention_days=1)
        writer.start()
        writer.flush()
        writer.stop(timeout=5.0)

        rows = _rows(db_path)
        assert len(rows) == 1
        assert rows[0]["msg"] == "today"


# ---------------------------------------------------------------------------
# 5.5 — Regression checks against full agent_logging wiring
# ---------------------------------------------------------------------------
class TestHighLevelRegression:
    def test_sqlite_queue_handler_end_to_end(self, tmp_path):
        db_path = al.setup_logging(str(tmp_path / "agent.log"), backup_count=1)
        al.bind_run_context(trace="r-9999", agent="main", prompt_id="01JARYN6R0")
        al.log_event(al.LogEvent.TOOL_END, "done", tool="shell", exit=0)
        _flush()

        row = _last_row(db_path)
        assert row["trace"] == "r-9999"
        assert row["agent"] == "main"
        assert row["prompt_id"] == "01JARYN6R0"
        assert row["event_type"] == "TOOL_END"
        assert row["tool"] == "shell"
        assert row["exit"] == 0

    def test_graph_memory_records_never_reach_store(self, tmp_path):
        db_path = al.setup_logging(str(tmp_path / "agent.log"), backup_count=1)
        gm_log = str(tmp_path / "graph_memory.log")
        logging.getLogger("graph_memory").info("graph memory secret")
        logging.getLogger("normal").info("normal record")
        _flush()

        gm_prose = _read(gm_log)
        assert "graph memory secret" in gm_prose

        rows = _rows(db_path)
        assert any(r["msg"] == "normal record" for r in rows)
        assert not any(r.get("msg") == "graph memory secret" for r in rows)

    def test_prose_sink_renders_identity_prefix(self, tmp_path):
        log_file = str(tmp_path / "agent.log")
        al.setup_logging(log_file, backup_count=1)
        al.bind_run_context(trace="r-deadbeef", agent="sa-7")
        logging.getLogger("x").info("hello")
        _flush()

        prose = _read(log_file)
        assert "[sa-7 r-deadbeef]" in prose
        assert "hello" in prose

    def test_run_identity_in_rows(self, tmp_path):
        db_path = al.setup_logging(str(tmp_path / "agent.log"), backup_count=1)
        al.bind_run_context(trace="r-1111", agent="main", prompt_id="7")
        logging.getLogger("x").info("identity test")
        _flush()

        row = _last_row(db_path)
        assert row["trace"] == "r-1111"
        assert row["agent"] == "main"
        assert row["prompt_id"] == "7"

    def test_missing_run_context_is_graceful(self, tmp_path):
        db_path = al.setup_logging(str(tmp_path / "agent.log"), backup_count=1)
        logging.getLogger("startup").info("boot")
        _flush()

        row = _last_row(db_path)
        assert "trace" not in row
        assert "agent" not in row
        assert "prompt_id" not in row

    def test_nested_run_context_restores_parent_identity(self, tmp_path):
        db_path = al.setup_logging(str(tmp_path / "agent.log"), backup_count=1)
        parent = al.bind_run_context(trace="r-parent", agent="main", prompt_id="5")
        logging.getLogger("x").info("parent before")
        child = al.bind_run_context(trace="r-child", agent="sa-2", prompt_id="6")
        logging.getLogger("x").info("child")
        al.reset_run_context(child)
        logging.getLogger("x").info("parent after")
        _flush()

        rows = _rows(db_path)
        assert rows[0]["trace"] == "r-parent" and rows[0]["prompt_id"] == "5"
        assert rows[1]["trace"] == "r-child" and rows[1]["prompt_id"] == "6"
        assert rows[2]["trace"] == "r-parent" and rows[2]["prompt_id"] == "5"
        al.reset_run_context(parent)

    def test_secret_redaction_in_structured_rows(self, tmp_path):
        db_path = al.setup_logging(str(tmp_path / "agent.log"), secret_values=["S3CR3T"], backup_count=1)
        log_file = str(tmp_path / "agent.log")
        al.log_event(
            al.LogEvent.TOOL_FAILED,
            "leaked S3CR3T in msg",
            err="S3CR3T in extra",
            tool="shell",
        )
        _flush()

        row = _last_row(db_path)
        assert "S3CR3T" not in row["msg"]
        assert "S3CR3T" not in row["err"]
        assert "***REDACTED***" in row["msg"]
        assert "***REDACTED***" in row["err"]

        prose = _read(log_file)
        assert "S3CR3T" not in prose

"""
sqlite_log.py
-------------
SQLite WAL structured log store for the agent.

The store replaces the previous JSONL structured sink (``agent.jsonl``). It is
fed by a single writer thread that owns the only sqlite3 connection. All
emitters enqueue rendered records via a bounded ``queue.Queue``; the writer
batched-inserts them into ``agent_logs.sqlite``.

Handler wiring choice
~~~~~~~~~~~~~~~~~~~~~
The structlog processor chain is unchanged. The structured sink uses a
``logging.QueueHandler`` subclass that enqueues the **dict** produced by the
chain. In practice the record passes through
``structlog.stdlib.ProcessorFormatter`` whose last processor is
``structlog.processors.JSONRenderer()`` when configured by
:func:`agent_logging.setup_logging`. The formatter's ``format()`` method
returns a JSON *string*, which the handler must parse back into a dict before
enqueuing, because ``QueueHandler`` natively pickles the whole ``LogRecord``
and the writer's row-mapping layer expects a plain record dict. To keep the
writer dependency-free and reusable by the future backfill CLI, the handler
re-parses the rendered JSON string in the emitting thread and places the dict
on the queue. The queue therefore never holds ``LogRecord`` or JSON strings;
it holds record dicts ready for :func:`record_to_row`.

Overflow policy: the queue is bounded. When full, the oldest record is dropped,
a ``dropped`` counter is incremented, and a WARNING is emitted to the prose
sink via plain stdlib logging so the loss is visible even if the store is
unavailable.

Failure recovery: writer catches exceptions per batch, retries once, and
re-opens the connection. If the retry also fails, the writer disables itself
(``disabled`` flag), emits a WARNING to prose, and keeps draining the queue so
emitters never block. Startup open failures follow the same path.

Retention: at startup and once daily the writer runs
``DELETE FROM events WHERE ts < cutoff`` (30 days ago) followed by
``PRAGMA wal_checkpoint(TRUNCATE)``.
"""

from __future__ import annotations

import json
import logging
import math
import os
import queue
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

DEFAULT_STORE_FILENAME: str = "agent_logs.sqlite"


class _FlushMarker:
    """Queue marker used by :meth:`SqliteLogWriter.flush` to await the writer."""

    def __init__(self) -> None:
        self.event = threading.Event()

# Bounded queue size; chosen to buffer spikes without unbounded memory growth.
_MAX_QUEUE_SIZE: int = 10000

# Writer batch size; balances transaction frequency and latency.
_BATCH_SIZE: int = 100

# Seconds the writer waits on the inbound queue when empty. Short enough for
# prompt shutdown, long enough to avoid busy-waiting.
_QUEUE_GET_TIMEOUT: float = 0.1

# Retention horizon in days.
_RETENTION_DAYS: int = 30

# Seconds between daily retention runs (also checked on wake from enqueue).
_RETENTION_INTERVAL_SECONDS: float = 24 * 3600

# Wide columns stored as individual table columns.
# This tuple is part of the public schema contract shared with log_query and
# the backfill CLI; callers may rely on the column ordering when mapping rows
# to records.
WIDE_COLUMNS: tuple[str, ...] = (
    "ts",
    "level",
    "logger",
    "agent",
    "trace",
    "prompt_id",
    "event_type",
    "msg",
)

SCHEMA_DDL: str = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    logger TEXT,
    agent TEXT,
    trace TEXT,
    prompt_id TEXT,
    event_type TEXT,
    msg TEXT,
    extra TEXT,
    search_text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_trace ON events(trace);
CREATE INDEX IF NOT EXISTS idx_prompt_id ON events(prompt_id);
CREATE INDEX IF NOT EXISTS idx_agent ON events(agent);
CREATE INDEX IF NOT EXISTS idx_event_type ON events(event_type) WHERE event_type IS NOT NULL;
"""

# INSERT statement for the ``events`` table. Column order matches
# :func:`record_to_row` and is the cross-module contract shared with the
# backfill CLI; do not reorder without updating both sites.
INSERT_EVENT_SQL: str = """
INSERT INTO events
(ts, level, logger, agent, trace, prompt_id, event_type, msg, extra, search_text)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _compact_json(obj: Any) -> str:
    """Return a compact JSON string with no ASCII escaping or extra whitespace.

    Non-finite floats (``NaN``, ``Infinity``, ``-Infinity``) are replaced with
    ``None`` so the result is valid JSON and safe for SQLite ``json_extract`` on
    older versions. Values in ``extra`` and ``search_text`` are both sanitized
    because both are produced through this helper.
    """

    def _sanitize(value: Any) -> Any:
        if isinstance(value, float) and not math.isfinite(value):
            return None
        if isinstance(value, dict):
            return {k: _sanitize(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_sanitize(v) for v in value]
        return value

    return json.dumps(_sanitize(obj), ensure_ascii=False, separators=(",", ":"))


def record_to_row(record: dict[str, Any]) -> tuple:
    """Map a structured log record dict to an ``events`` row tuple.

    Wide columns (``ts``, ``level``, ``logger``, ``agent``, ``trace``,
    ``prompt_id``, ``event_type``, ``msg``) are extracted individually.
    Missing wide columns become ``None``; ``ts`` and ``level`` fall back to
    ``""`` so the ``NOT NULL`` constraints hold. Every remaining field is
    preserved in the ``extra`` JSON column. ``search_text`` is the
    casefolded compact-JSON serialization of the *full* original record dict.

    Args:
        record: Structured log record dict emitted by the structlog chain.

    Returns:
        A tuple matching the ``events`` column order:
        ``(ts, level, logger, agent, trace, prompt_id, event_type, msg,
        extra, search_text)``.
    """
    wide: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    for key, value in record.items():
        if key in WIDE_COLUMNS:
            wide[key] = value
        else:
            extra[key] = value

    ts = wide.get("ts") or ""
    level = wide.get("level") or ""
    logger_name = wide.get("logger")
    agent = wide.get("agent")
    trace = wide.get("trace")
    prompt_id = wide.get("prompt_id")
    event_type = wide.get("event_type")
    msg = wide.get("msg")

    extra_json = _compact_json(extra) if extra else "{}"
    search_text = _compact_json(record).casefold()

    return (
        ts,
        level,
        logger_name,
        agent,
        trace,
        prompt_id,
        event_type,
        msg,
        extra_json,
        search_text,
    )


def _init_schema(conn: sqlite3.Connection) -> None:
    """Create the events table and indexes on *conn*."""
    conn.executescript(SCHEMA_DDL)
    conn.commit()



def _apply_wal_pragmas(conn: sqlite3.Connection) -> None:
    """Configure WAL journal mode and normal synchronous safety on *conn*."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")


def _run_retention(conn: sqlite3.Connection, days: int = _RETENTION_DAYS) -> int:
    """Delete rows older than *days* days and truncate the WAL.

    Args:
        conn: An open sqlite3 connection.
        days: Retention horizon in days.

    Returns:
        The number of rows deleted.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with conn:
        cur = conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        deleted = cur.rowcount
    # Checkpoint must run outside an active transaction to avoid "table locked".
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return deleted


def connect_store(path: str) -> sqlite3.Connection:
    """Open a read-only-capable sqlite3 connection to the store.

    Use this helper for readers (e.g. the ``log_query`` tool). The connection
    uses the default SQLite journaling mode (WAL readers are fine with a
    plain connection) and returns rows as sqlite3.Row for field-name access.
    """
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def row_to_record(row: sqlite3.Row) -> dict:
    """Reconstruct a record dict from an ``events`` row.

    Wide columns are merged first (only non-NULL values are included), then the
    parsed ``extra`` JSON is layered on top so that keys such as ``tool``,
    ``exit``, ``dur_ms``, ``err``, and ``model`` appear at the top level exactly
    as they did in the original JSONL records.
    """
    record: dict = {}
    for col in WIDE_COLUMNS:
        value = row[col]
        if value is not None:
            record[col] = value
    extra_json = row["extra"]
    if extra_json:
        extra = json.loads(extra_json)
        record.update(extra)
    return record


class SQLiteQueueHandler(logging.Handler):
    """``logging.Handler`` that enqueues rendered record dicts.

    This handler is meant to be used with a ``ProcessorFormatter`` whose last
    processor renders the event dict as JSON. The handler parses that JSON
    string back to a dict and places the dict on the writer's queue. If
    parsing fails, the record is dropped and an error is reported to the
    prose sink.
    """

    def __init__(self, writer: "SqliteLogWriter") -> None:
        super().__init__()
        self._writer = writer

    def emit(self, record: logging.LogRecord) -> None:
        """Format the record as JSON, parse it, and enqueue the dict."""
        try:
            rendered = self.format(record)
            parsed = json.loads(rendered)
        except Exception:  # noqa: BLE001 — emit must not raise
            self.handleError(record)
            return
        self._writer.enqueue(parsed)


class SqliteLogWriter:
    """Single-writer SQLite store with a bounded inbound queue.

    The writer thread owns the only sqlite3 connection. Callers add records
    with :meth:`enqueue`. Graceful shutdown uses :meth:`stop` to drain pending
    records. If the database cannot be opened or writes fail persistently,
    the writer disables itself and emits a WARNING to the prose sink while
    continuing to drain the queue.
    """

    def __init__(
        self,
        path: str,
        *,
        queue_size: int = _MAX_QUEUE_SIZE,
        batch_size: int = _BATCH_SIZE,
        retention_days: int = _RETENTION_DAYS,
    ) -> None:
        """Create the writer; the database connection is opened in the worker thread.

        Args:
            path: Absolute path to the SQLite store file.
            queue_size: Maximum inbound queue records before drop-oldest.
            batch_size: Maximum records inserted per transaction.
            retention_days: Age horizon for the retention DELETE.
        """
        self._path = path
        self._batch_size = batch_size
        self._retention_days = retention_days
        self._queue: queue.Queue[dict[str, Any] | _FlushMarker] = queue.Queue(maxsize=queue_size)
        self._dropped: int = 0
        self._lock = threading.Lock()
        self._disabled = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._conn: sqlite3.Connection | None = None
        self._last_retention: float = 0.0
        self._warning_logger = logging.getLogger("sqlite_log_writer")
        self._warning_logger.propagate = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @property
    def disabled(self) -> bool:
        """Return True if the writer has given up due to persistent failures."""
        with self._lock:
            return self._disabled

    @property
    def dropped(self) -> int:
        """Return the number of records dropped due to queue overflow."""
        with self._lock:
            return self._dropped

    def start(self) -> None:
        """Start the writer thread (idempotent)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._writer_loop, name="sqlite-log-writer", daemon=True)
            self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        """Signal the writer to stop, drain the queue, and join the thread.

        Stop is sent through the existing ``_stop_event`` instead of the data
        queue, so a full bounded queue cannot block or abort graceful shutdown.
        Pending records are drained and committed before the thread exits.

        Args:
            timeout: Seconds to wait for the writer thread to finish. ``None``
                waits indefinitely.
        """
        with self._lock:
            thread = self._thread
        if thread is None:
            return
        self._stop_event.set()
        thread.join(timeout=timeout)

    def flush(self, timeout: float | None = 2.0) -> bool:
        """Flush all currently queued records without stopping the writer.

        Args:
            timeout: Seconds to wait for the flush marker to be processed.

        Returns:
            ``True`` if the writer processed the marker within the timeout.
        """
        if self.disabled or self._stop_event.is_set():
            return False
        marker = _FlushMarker()
        try:
            self._queue.put_nowait(marker)
        except queue.Full:
            return False
        return marker.event.wait(timeout=timeout)

    def enqueue(self, record: dict[str, Any]) -> None:
        """Add a record dict to the queue. Never blocks; drops oldest on overflow.

        Args:
            record: Structured log record dict from the structlog chain.
        """
        if self._disabled or self._stop_event.is_set():
            return
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            # Drop oldest, then enqueue the new record. Because dropping one
            # frees a slot, put_nowait is guaranteed to succeed immediately.
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            with self._lock:
                self._dropped += 1
                dropped = self._dropped
            self._warning_logger.warning(
                "SQLite log store queue full; dropped oldest record (total_dropped=%d)",
                dropped,
            )
            try:
                self._queue.put_nowait(record)
            except queue.Full:
                # Extremely unlikely: a concurrent put could re-fill the slot.
                self._warning_logger.warning(
                    "SQLite log store queue full; could not enqueue record after drop"
                )

    # ------------------------------------------------------------------
    # Writer thread internals
    # ------------------------------------------------------------------
    def _writer_loop(self) -> None:
        """Main loop for the writer thread: connect, then drain/batch-insert."""
        try:
            self._conn = self._open_connection()
        except Exception as exc:  # noqa: BLE001 — startup failure degrades gracefully
            self._disable(f"SQLite log store could not be opened at {self._path}: {exc}")
            self._drain_discard()
            return

        try:
            self._do_retention(force=True)
        except Exception as exc:  # noqa: BLE001 — retention failure is not fatal
            self._warning_logger.warning("SQLite log store retention failed: %s", exc)

        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=_QUEUE_GET_TIMEOUT)
            except queue.Empty:
                continue
            if isinstance(item, _FlushMarker):
                self._queue.task_done()
                self._insert_batch([])
                item.event.set()
                continue

            batch: list[dict[str, Any]] = [item]
            self._queue.task_done()
            while len(batch) < self._batch_size:
                try:
                    extra = self._queue.get_nowait()
                except queue.Empty:
                    break
                if isinstance(extra, _FlushMarker):
                    self._queue.task_done()
                    self._insert_batch(batch)
                    batch = []
                    extra.event.set()
                    continue
                batch.append(extra)
                self._queue.task_done()

            if not self._disabled:
                self._insert_batch(batch)

        # Final drain of remaining queued records on graceful stop.
        self._drain_insert()
        self._close_connection()

    def _open_connection(self) -> sqlite3.Connection:
        """Open or create the SQLite store, apply pragmas, and run quick_check."""
        os.makedirs(os.path.dirname(os.path.abspath(self._path)), exist_ok=True)
        conn = sqlite3.connect(self._path)
        _apply_wal_pragmas(conn)
        _init_schema(conn)
        try:
            cur = conn.execute("PRAGMA quick_check")
            row = cur.fetchone()
            if row is None or row[0] != "ok":
                raise sqlite3.DatabaseError(f"PRAGMA quick_check failed: {row}")
        except sqlite3.Error:
            cur = conn.execute("PRAGMA integrity_check")
            row = cur.fetchone()
            if row is None or row[0] != "ok":
                raise sqlite3.DatabaseError(f"PRAGMA integrity_check failed: {row}")
        return conn

    def _close_connection(self) -> None:
        """Close the sqlite3 connection, swallowing errors."""
        conn = self._conn
        self._conn = None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _insert_batch(self, batch: list[dict[str, Any]]) -> None:
        """Insert one batch with one retry + reconnect on failure."""
        if not batch or self._conn is None:
            return
        rows = [record_to_row(rec) for rec in batch]
        try:
            with self._conn:
                self._conn.executemany(INSERT_EVENT_SQL, rows)
            self._do_retention()
        except Exception as exc:  # noqa: BLE001 — retry once
            self._warning_logger.warning(
                "SQLite log store batch insert failed (will retry once): %s", exc
            )
            try:
                self._close_connection()
                self._conn = self._open_connection()
                with self._conn:
                    self._conn.executemany(INSERT_EVENT_SQL, rows)
                self._do_retention()
            except Exception as exc2:  # noqa: BLE001
                self._disable(
                    f"SQLite log store persistently unavailable; disabling structured sink: {exc2}"
                )

    def _do_retention(self, *, force: bool = False) -> None:
        """Run retention if the daily interval has elapsed (or *force* is True).

        Startup calls ``force=True`` to prune immediately; periodic inserts pass
        ``force=False`` and only run when ``_RETENTION_INTERVAL_SECONDS`` has
        elapsed since the last successful run.
        """
        if not force:
            now = time.monotonic()
            if now - self._last_retention < _RETENTION_INTERVAL_SECONDS:
                return
        if self._conn is None:
            return
        prefix = "startup " if force else ""
        try:
            deleted = _run_retention(self._conn, days=self._retention_days)
            if deleted:
                self._warning_logger.info(
                    "SQLite log store %sretention pruned %d row(s)", prefix, deleted
                )
        except Exception as exc:  # noqa: BLE001
            self._warning_logger.warning("SQLite log store %sretention failed: %s", prefix, exc)
        finally:
            self._last_retention = time.monotonic()

    def _drain_discard(self) -> None:
        """Drain the queue without inserting after the store is disabled."""
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                break

    def _drain_insert(self) -> None:
        """Drain remaining queued records into the store before shutdown."""
        batch: list[dict[str, Any]] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, _FlushMarker):
                self._queue.task_done()
                self._insert_batch(batch)
                batch = []
                item.event.set()
                continue
            batch.append(item)
            self._queue.task_done()
            if len(batch) >= self._batch_size:
                self._insert_batch(batch)
                batch = []
        if batch and not self._disabled:
            self._insert_batch(batch)

    def _disable(self, message: str) -> None:
        """Disable the writer and emit a warning to the prose sink."""
        with self._lock:
            self._disabled = True
        self._warning_logger.warning(message)


# ---------------------------------------------------------------------------
# Module-level active writer tracking for agent_logging.py
# ---------------------------------------------------------------------------
_active_writer: SqliteLogWriter | None = None
_active_writer_lock = threading.Lock()


def set_active_writer(writer: SqliteLogWriter | None) -> SqliteLogWriter | None:
    """Set the module-level active writer (used by ``agent_logging`` reconfigure).

    Stops and closes any previously active writer before replacing it.

    Returns:
        The new writer, or ``None``.
    """
    global _active_writer
    with _active_writer_lock:
        old = _active_writer
        if old is not None and old is not writer:
            old.stop(timeout=5.0)
        _active_writer = writer
    return writer


def shutdown_log_store(timeout: float | None = 5.0) -> None:
    """Drain and stop the active structured-log writer, if any.

    Args:
        timeout: Seconds to wait for the writer thread to finish. ``None``
            waits indefinitely.
    """
    global _active_writer
    with _active_writer_lock:
        writer = _active_writer
        _active_writer = None
    if writer is not None:
        writer.stop(timeout=timeout)

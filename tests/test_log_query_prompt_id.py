"""Tests for ``log_query`` prompt_id filtering against the SQLite store."""

from __future__ import annotations

import json
import sqlite3

import pytest
import structlog

from sqlite_log import SCHEMA_DDL, record_to_row


def _insert_records(path: str, records: list[dict]) -> None:
    """Create the SQLite store schema and insert *records* as rows."""
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_DDL)
    if records:
        rows = [record_to_row(rec) for rec in records]
        conn.executemany(
            """
            INSERT INTO events
            (ts, level, logger, agent, trace, prompt_id, event_type, msg, extra, search_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    conn.commit()
    conn.close()


@pytest.fixture
def executor(make_builtin_executor, tmp_path):
    """A BuiltinExecutor wired to a temp SQLite store."""
    log_store_path = str(tmp_path / "agent_logs.sqlite")
    _insert_records(log_store_path, [])
    exc = make_builtin_executor(log_store_path=log_store_path)
    yield exc


@pytest.fixture(autouse=True)
def _clear_contextvars():
    """Keep structlog contextvars isolated between tests."""
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


def _write_records(path: str, records: list[dict]) -> None:
    """Overwrite *path* with a fresh schema and *records*."""
    _insert_records(path, records)


class TestPromptIdFilter:
    def test_filter_by_prompt_id_returns_only_matches(self, executor, tmp_path):
        records = [
            {"ts": "2026-07-05T10:00:00", "trace": "r-1", "prompt_id": "01JARYN6R0ABCDEFGHJKMNPQRS", "level": "info", "event_type": "TOOL_START", "msg": "a"},
            {"ts": "2026-07-05T10:00:01", "trace": "r-2", "prompt_id": "01JARYZ3W2ABCDEFGHJKMNPQRS", "level": "info", "event_type": "TOOL_START", "msg": "b"},
            {"ts": "2026-07-05T10:00:02", "trace": "r-3", "prompt_id": "01JARYN6R0ABCDEFGHJKMNPQRS", "level": "info", "event_type": "TOOL_START", "msg": "c"},
        ]
        _write_records(executor._log_store_path, records)

        result = executor._logquery._exec_log_query({"prompt_id": "01JARYN6R0ABCDEFGHJKMNPQRS", "trace": "*"})
        assert result["success"] is True
        payload = json.loads(result["output"])
        assert payload["count"] == 2
        assert {r["msg"] for r in payload["records"]} == {"a", "c"}

    def test_prompt_id_combines_with_trace_filter(self, executor, tmp_path):
        records = [
            {"ts": "2026-07-05T10:00:00", "trace": "r-1", "prompt_id": "01JARYN6R0ABCDEFGHJKMNPQRS", "level": "info", "event_type": "TOOL_START", "msg": "a"},
            {"ts": "2026-07-05T10:00:01", "trace": "r-2", "prompt_id": "01JARYZ3W2ABCDEFGHJKMNPQRS", "level": "info", "event_type": "TOOL_START", "msg": "b"},
            {"ts": "2026-07-05T10:00:02", "trace": "r-1", "prompt_id": "01JARYZ3W2ABCDEFGHJKMNPQRS", "level": "info", "event_type": "TOOL_START", "msg": "c"},
        ]
        _write_records(executor._log_store_path, records)

        result = executor._logquery._exec_log_query({"prompt_id": "01JARYZ3W2ABCDEFGHJKMNPQRS", "trace": "r-2"})
        assert result["success"] is True
        payload = json.loads(result["output"])
        assert payload["count"] == 1
        assert payload["records"][0]["msg"] == "b"

    def test_prompt_id_combines_with_level_and_event(self, executor, tmp_path):
        records = [
            {"ts": "2026-07-05T10:00:00", "trace": "r-1", "prompt_id": "01JARYN6R0ABCDEFGHJKMNPQRS", "level": "warning", "event_type": "TOOL_FAILED", "msg": "a"},
            {"ts": "2026-07-05T10:00:01", "trace": "r-1", "prompt_id": "01JARYN6R0ABCDEFGHJKMNPQRS", "level": "info", "event_type": "TOOL_START", "msg": "b"},
            {"ts": "2026-07-05T10:00:02", "trace": "r-1", "prompt_id": "01JARYZ3W2ABCDEFGHJKMNPQRS", "level": "warning", "event_type": "TOOL_FAILED", "msg": "c"},
        ]
        _write_records(executor._log_store_path, records)

        result = executor._logquery._exec_log_query(
            {"prompt_id": "01JARYN6R0ABCDEFGHJKMNPQRS", "level": "WARNING", "event_type": "TOOL_FAILED"},
        )
        assert result["success"] is True
        payload = json.loads(result["output"])
        assert payload["count"] == 1
        assert payload["records"][0]["msg"] == "a"

    def test_empty_result_is_well_formed(self, executor, tmp_path):
        records = [
            {"ts": "2026-07-05T10:00:00", "trace": "r-1", "prompt_id": "01JARYN6R0ABCDEFGHJKMNPQRS", "level": "info", "msg": "a"},
        ]
        _write_records(executor._log_store_path, records)

        result = executor._logquery._exec_log_query({"prompt_id": "01JNONEXISTENTULIDSTRING00000"})
        assert result["success"] is True
        payload = json.loads(result["output"])
        assert payload["records"] == []
        assert payload["count"] == 0
        assert payload["truncated"] is False

    def test_prompt_id_without_trace_auto_widens(self, executor, tmp_path):
        # Reproduces the bug where an omitted trace defaulted to the current
        # run's trace and hid the target prompt's records.
        records = [
            {"ts": "2026-07-05T10:00:00", "trace": "r-target", "prompt_id": "01TARGETABCDEFGHJKMNPQRSTUV", "level": "info", "event_type": "TOOL_START", "msg": "target-a"},
            {"ts": "2026-07-05T10:00:01", "trace": "r-target", "prompt_id": "01TARGETABCDEFGHJKMNPQRSTUV", "level": "info", "event_type": "TOOL_END", "msg": "target-b"},
            {"ts": "2026-07-05T10:00:02", "trace": "r-current", "prompt_id": "01CURRENTABCDEFGHJKMNPQRS", "level": "info", "event_type": "TOOL_START", "msg": "current-a"},
        ]
        _write_records(executor._log_store_path, records)

        structlog.contextvars.bind_contextvars(trace="r-current")
        try:
            result = executor._logquery._exec_log_query({"prompt_id": "01TARGETABCDEFGHJKMNPQRSTUV"})
        finally:
            structlog.contextvars.clear_contextvars()
        assert result["success"] is True
        payload = json.loads(result["output"])
        assert payload["count"] == 2
        assert {r["msg"] for r in payload["records"]} == {"target-a", "target-b"}


    def test_prompt_id_unambiguous_across_days(self, executor, tmp_path):
        # Spec scenario "Filter by prompt id is unambiguous across days": one
        # prompt ULID's records span multiple storage days; querying it today
        # returns all of them (no day boundary in the store), with no
        # collision from a different ULID.
        records = [
            {"ts": "2026-07-03T10:00:00", "trace": "r-1", "prompt_id": "01JARYN6R0ABCDEFGHJKMNPQRS", "level": "info", "event_type": "RUN_BEGIN", "msg": "day one"},
            {"ts": "2026-07-05T09:00:00", "trace": "r-9", "prompt_id": "01JARYN6R0ABCDEFGHJKMNPQRS", "level": "info", "event_type": "TOOL_START", "msg": "day three"},
            {"ts": "2026-07-05T10:00:01", "trace": "r-2", "prompt_id": "01JARYZ3W2ABCDEFGHJKMNPQRS", "level": "info", "event_type": "TOOL_START", "msg": "other prompt"},
        ]
        _write_records(executor._log_store_path, records)

        # level=DEBUG disables the Option C default view so the prompt_id
        # filter is isolated (RUN_BEGIN is not in the six-event default set).
        result = executor._logquery._exec_log_query(
            {"prompt_id": "01JARYN6R0ABCDEFGHJKMNPQRS", "trace": "*", "level": "DEBUG"}
        )
        assert result["success"] is True
        payload = json.loads(result["output"])
        assert payload["count"] == 2
        assert {r["msg"] for r in payload["records"]} == {"day one", "day three"}
        assert "other prompt" not in json.dumps(payload)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

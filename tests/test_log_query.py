"""Tests for the ``log_query`` built-in tool and its structured-log filtering.

Covers trace filtering, the current-run-trace default, the Option C default
view (STEP_* excluded / TOOL_*/LLM_CALL & WARNING+ included), the explicit
level/event_type/tool/since filters, limit/truncation, graceful handling of
malformed lines, and the empty result when the log path is missing/unset.
"""

from __future__ import annotations

import json

import pytest
import structlog

from builtin_executor import (
    BuiltinExecutor,
    _LOG_QUERY_MAX_SCAN_LINES,
    _LOG_QUERY_TAIL_BYTES,
)

TRACE_A = "r-aaaaaaaa"
TRACE_B = "r-bbbbbbbb"


@pytest.fixture(autouse=True)
def _clear_contextvars():
    """Keep structlog contextvars isolated between tests."""
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


@pytest.fixture
def sample_records():
    """Eight records spanning traces, levels, event types, tools, timestamps."""
    return [
        {"ts": "2026-07-05T10:00:00", "trace": TRACE_A, "level": "info",
         "event_type": "TOOL_START", "tool": "shell"},
        {"ts": "2026-07-05T10:00:01", "trace": TRACE_A, "level": "info",
         "event_type": "TOOL_END", "tool": "shell", "exit": 0, "dur_ms": 5},
        {"ts": "2026-07-05T10:00:02", "trace": TRACE_A, "level": "info",
         "event_type": "STEP_BEGIN"},
        {"ts": "2026-07-05T10:00:03", "trace": TRACE_A, "level": "info",
         "event_type": "STEP_END"},
        {"ts": "2026-07-05T10:00:04", "trace": TRACE_A, "level": "info",
         "event_type": "LLM_CALL", "model": "opus"},
        {"ts": "2026-07-05T10:00:05", "trace": TRACE_A, "level": "error",
         "event_type": "TOOL_FAILED", "tool": "file_read", "exit": -1, "err": "boom"},
        {"ts": "2026-07-05T10:00:06", "trace": TRACE_B, "level": "warning",
         "event_type": "STEP_END"},
        {"ts": "2026-07-05T10:00:07", "trace": TRACE_B, "level": "info",
         "event_type": "TOOL_START", "tool": "schedule"},
    ]


@pytest.fixture
def log_path(tmp_path, sample_records):
    """Write the sample records with interleaved malformed lines to a .jsonl."""
    path = tmp_path / "agent.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("this is not json at all\n")   # malformed -> skipped
        fh.write("\n")                            # blank -> skipped
        for rec in sample_records:
            fh.write(json.dumps(rec) + "\n")
        fh.write("{ still not: valid json\n")     # malformed tail -> skipped
    return path


@pytest.fixture
def executor(log_path):
    return BuiltinExecutor(log_jsonl_path=str(log_path))


def _query(exc, **filters):
    """Invoke log_query through the public execute() dispatch and parse output."""
    result = exc.execute("log_query", filters)
    assert result["success"] is True
    assert result["error"] == ""
    assert result["exit_code"] == 0
    return json.loads(result["output"])


def test_registered_as_builtin(executor):
    assert executor.is_builtin("log_query")
    assert "log_query" in {t.name for t in executor.all_tools()}


def test_trace_filtering(executor):
    # level=DEBUG includes every level, isolating the trace filter from the
    # Option C default view.
    payload_a = _query(executor, trace=TRACE_A, level="DEBUG")
    assert payload_a["total_matched"] == 6
    assert payload_a["count"] == 6
    assert {r["trace"] for r in payload_a["records"]} == {TRACE_A}

    payload_b = _query(executor, trace=TRACE_B, level="DEBUG")
    assert payload_b["total_matched"] == 2
    assert {r["trace"] for r in payload_b["records"]} == {TRACE_B}


def test_current_run_trace_default(executor):
    # No trace arg -> default to the current run's bound trace.
    structlog.contextvars.bind_contextvars(trace=TRACE_B)
    try:
        payload = _query(executor, level="DEBUG")
    finally:
        structlog.contextvars.clear_contextvars()
    assert payload["total_matched"] == 2
    assert {r["trace"] for r in payload["records"]} == {TRACE_B}

    # With no bound trace and no arg, the empty default widens to all traces.
    payload_all = _query(executor, level="DEBUG")
    assert payload_all["total_matched"] == 8


def test_wildcard_trace_matches_all(executor):
    payload = _query(executor, trace="*", level="DEBUG")
    assert payload["total_matched"] == 8


def test_default_view_option_c(executor):
    # Neither level nor event_type -> Option C default view.
    payload = _query(executor, trace="*")
    records = payload["records"]
    event_types = [r["event_type"] for r in records]

    # High-signal lifecycle events and WARNING+ are kept.
    assert "TOOL_START" in event_types
    assert "TOOL_END" in event_types
    assert "LLM_CALL" in event_types
    assert "TOOL_FAILED" in event_types  # error level satisfies WARNING+

    # Routine STEP_BEGIN/STEP_END (INFO) are dropped ...
    assert "STEP_BEGIN" not in event_types
    assert not any(
        r["event_type"] == "STEP_END" and r["level"] == "info" for r in records
    )
    # ... but a WARNING+ STEP_END is retained.
    assert any(
        r["event_type"] == "STEP_END" and r["level"] == "warning" for r in records
    )

    assert payload["total_matched"] == 6


def test_level_filter(executor):
    payload = _query(executor, trace="*", level="ERROR")
    assert payload["total_matched"] == 1
    assert payload["records"][0]["event_type"] == "TOOL_FAILED"
    assert payload["records"][0]["level"] == "error"


def test_event_type_filter(executor):
    payload = _query(executor, trace="*", event_type="TOOL_START")
    assert payload["total_matched"] == 2
    assert {r["event_type"] for r in payload["records"]} == {"TOOL_START"}
    assert {r["tool"] for r in payload["records"]} == {"shell", "schedule"}


def test_tool_filter(executor):
    payload = _query(executor, trace="*", tool="shell", level="DEBUG")
    assert payload["total_matched"] == 2
    assert {r["tool"] for r in payload["records"]} == {"shell"}


def test_since_filter(executor):
    payload = _query(executor, trace="*", level="DEBUG", since="2026-07-05T10:00:05")
    assert payload["total_matched"] == 3
    assert all(r["ts"] >= "2026-07-05T10:00:05" for r in payload["records"])


def test_limit_and_truncation(executor):
    payload = _query(executor, trace="*", level="DEBUG", limit=3)
    assert payload["total_matched"] == 8
    assert payload["truncated"] is True
    assert payload["count"] == 3
    # The most recent records are kept.
    assert [r["ts"] for r in payload["records"]] == [
        "2026-07-05T10:00:05",
        "2026-07-05T10:00:06",
        "2026-07-05T10:00:07",
    ]


def test_no_truncation_when_under_limit(executor):
    payload = _query(executor, trace="*", level="DEBUG", limit=50)
    assert payload["truncated"] is False
    assert payload["count"] == 8
    assert payload["total_matched"] == 8


def test_malformed_lines_skipped(executor):
    # The log file contains malformed/blank lines; a DEBUG all-trace query still
    # returns exactly the 8 well-formed records.
    payload = _query(executor, trace="*", level="DEBUG")
    assert payload["total_matched"] == 8


def test_missing_file_returns_empty(tmp_path):
    exc = BuiltinExecutor(log_jsonl_path=str(tmp_path / "does_not_exist.jsonl"))
    result = exc.execute("log_query", {})
    assert result["success"] is True
    payload = json.loads(result["output"])
    assert payload["records"] == []
    assert payload["count"] == 0
    assert payload["truncated"] is False
    assert payload["total_matched"] == 0
    assert payload["window_saturated"] is False
    assert payload["scanned_lines"] == 0


def test_unset_path_returns_empty():
    exc = BuiltinExecutor()  # log_jsonl_path defaults to ""
    result = exc.execute("log_query", {})
    assert result["success"] is True
    payload = json.loads(result["output"])
    assert payload["records"] == []
    assert payload["total_matched"] == 0
    assert payload["window_saturated"] is False
    assert payload["scanned_lines"] == 0


def test_window_fields_present_not_saturated(executor, sample_records):
    """Small file: the disclosure fields are present, the window is not
    saturated, and scanned_lines counts every physical tail line (records plus
    the interleaved malformed/blank lines)."""
    payload = _query(executor, trace="*", level="DEBUG")
    assert payload["window_saturated"] is False
    # The log_path fixture writes the 8 records plus 3 non-record lines
    # (2 malformed + 1 blank), all inside the (tiny) tail window.
    assert payload["scanned_lines"] == len(sample_records) + 3
    assert payload["total_matched"] == 8


def test_window_saturation_line_cap(tmp_path):
    """More lines than the scan cap -> window_saturated, scanned_lines bounded to
    the line cap, and total_matched is a recent-window count (< the file total)."""
    path = tmp_path / "many.jsonl"
    n = _LOG_QUERY_MAX_SCAN_LINES + 300
    with open(path, "w", encoding="utf-8") as fh:
        for i in range(n):
            fh.write(json.dumps({
                "ts": f"{i:08d}", "trace": TRACE_A, "level": "info",
                "event_type": "TOOL_END", "tool": "shell",
            }) + "\n")
    exc = BuiltinExecutor(log_jsonl_path=str(path))
    payload = _query(exc, trace=TRACE_A, event_type="TOOL_END", limit=10)
    assert payload["window_saturated"] is True
    assert payload["scanned_lines"] == _LOG_QUERY_MAX_SCAN_LINES
    # total_matched reflects only the scanned tail, not the whole file.
    assert payload["total_matched"] == _LOG_QUERY_MAX_SCAN_LINES
    assert payload["total_matched"] < n
    # The most recent record is retained despite older ones falling outside.
    assert payload["records"][-1]["ts"] == f"{n - 1:08d}"


def test_window_saturation_byte_cap(tmp_path):
    """Fewer lines than the line cap but more bytes than the byte cap: the read
    begins mid-file, so window_saturated is True and older lines are dropped."""
    path = tmp_path / "wide.jsonl"
    pad = "x" * 400
    n_lines = 4000
    with open(path, "w", encoding="utf-8") as fh:
        for i in range(n_lines):
            fh.write(json.dumps({
                "ts": f"{i:08d}", "trace": TRACE_A, "level": "info",
                "event_type": "TOOL_END", "tool": "shell", "pad": pad,
            }) + "\n")
    assert path.stat().st_size > _LOG_QUERY_TAIL_BYTES   # sanity: exceeds byte window
    assert n_lines < _LOG_QUERY_MAX_SCAN_LINES           # so the line cap is NOT the trigger
    exc = BuiltinExecutor(log_jsonl_path=str(path))
    payload = _query(exc, trace=TRACE_A, event_type="TOOL_END", limit=5)
    assert payload["window_saturated"] is True
    assert 0 < payload["scanned_lines"] < n_lines        # older lines outside the byte window
    assert payload["scanned_lines"] <= _LOG_QUERY_MAX_SCAN_LINES

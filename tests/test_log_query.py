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


# ---------------------------------------------------------------------------
# text / query argument — new bounded text search
# ---------------------------------------------------------------------------

@pytest.fixture
def text_search_executor(tmp_path):
    """Executor with a log containing records that span traces, levels, and a
    distinctive INFO startup message dropped by the Option C default view."""
    records = [
        # TRACE_A — two TOOL_START events
        {"ts": "2026-07-05T10:00:00", "trace": TRACE_A, "level": "info",
         "event_type": "TOOL_START", "tool": "shell"},
        {"ts": "2026-07-05T10:00:01", "trace": TRACE_A, "level": "info",
         "event_type": "TOOL_START", "tool": "schedule"},
        # TRACE_A — INFO startup message that Option C default view drops
        {"ts": "2026-07-05T10:00:02", "trace": TRACE_A, "level": "info",
         "event_type": "STEP_BEGIN",
         "msg": "GraphMemoryStore initialised at data/graph_memory (dim=1536)"},
        # TRACE_B — one TOOL_START event
        {"ts": "2026-07-05T10:00:03", "trace": TRACE_B, "level": "info",
         "event_type": "TOOL_START", "tool": "file_read"},
    ]
    path = tmp_path / "text.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    return BuiltinExecutor(log_jsonl_path=str(path))


def test_text_search_finds_dropped_info_record(text_search_executor):
    """text= finds an INFO STEP_BEGIN record that the default view would drop."""
    # Verify that default view drops the record.
    default_payload = _query(text_search_executor, trace="*")
    default_events = [r["event_type"] for r in default_payload["records"]]
    assert "STEP_BEGIN" not in default_events

    # text= should surface the same record.
    payload = _query(text_search_executor, trace="*", text="GraphMemoryStore")
    assert payload["total_matched"] == 1
    assert payload["records"][0]["event_type"] == "STEP_BEGIN"
    assert "GraphMemoryStore" in payload["records"][0]["msg"]


def test_query_alias_behaves_same(text_search_executor):
    """query= alias produces the same results as text=."""
    payload_text = _query(text_search_executor, trace="*", text="GraphMemoryStore")
    payload_query = _query(text_search_executor, trace="*", query="GraphMemoryStore")
    assert payload_text["total_matched"] == payload_query["total_matched"]
    assert payload_text["records"] == payload_query["records"]


def test_text_search_case_insensitive(text_search_executor):
    """text= search is case-insensitive."""
    payload_upper = _query(text_search_executor, trace="*", text="GRAPHMEMORYSTORE")
    payload_lower = _query(text_search_executor, trace="*", text="graphmemorystore")
    payload_mixed = _query(text_search_executor, trace="*", text="GraphMemoryStore")
    assert payload_upper["total_matched"] == payload_lower["total_matched"] == payload_mixed["total_matched"]
    assert payload_upper["records"] == payload_lower["records"] == payload_mixed["records"]
    assert payload_upper["total_matched"] == 1


def test_text_search_auto_widens_without_explicit_trace(text_search_executor):
    """Without an explicit trace, a text search auto-widens to all traces.

    Startup records may carry no trace or a different trace; the auto-widen
    ensures they are found by a bare {"text": "…"} call regardless of the
    current-run contextvars binding.
    """
    # Binding TRACE_B should NOT restrict results: auto-widen ignores contextvars.
    structlog.contextvars.bind_contextvars(trace=TRACE_B)
    try:
        payload = _query(text_search_executor, text="TOOL_START")
    finally:
        structlog.contextvars.clear_contextvars()
    # TRACE_A has 2, TRACE_B has 1 — all three are found.
    assert payload["total_matched"] == 3
    assert {r["trace"] for r in payload["records"]} == {TRACE_A, TRACE_B}


def test_text_search_explicit_trace_scopes(text_search_executor):
    """An explicit trace restricts the text search to that trace only."""
    payload = _query(text_search_executor, trace=TRACE_B, text="TOOL_START")
    assert payload["total_matched"] == 1
    assert all(r["trace"] == TRACE_B for r in payload["records"])


def test_text_search_wildcard_finds_all_traces(text_search_executor):
    """With trace='*', text search covers all traces."""
    payload = _query(text_search_executor, trace="*", text="TOOL_START")
    # TRACE_A has two TOOL_START records, TRACE_B has one.
    assert payload["total_matched"] == 3
    assert {r["trace"] for r in payload["records"]} == {TRACE_A, TRACE_B}


# ---------------------------------------------------------------------------
# Regression: auto-widen finds startup records with no trace field
# ---------------------------------------------------------------------------

@pytest.fixture
def no_trace_startup_executor(tmp_path):
    """Log with a traceless startup record and a current-run record on TRACE_A.

    Simulates the real scenario: the process emits INFO messages before the
    trace ID is bound to contextvars, so those records carry no 'trace' key.
    """
    records = [
        # Traceless startup record — emitted before trace ID is available.
        {"ts": "2026-07-05T09:59:58", "level": "info",
         "event_type": "RUN_BEGIN",
         "msg": "GraphMemoryStore initialised at data/graph_memory (dim=1536)"},
        # Same-run record on TRACE_A.
        {"ts": "2026-07-05T10:00:00", "trace": TRACE_A, "level": "info",
         "event_type": "TOOL_START", "tool": "shell"},
    ]
    path = tmp_path / "startup.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    return BuiltinExecutor(log_jsonl_path=str(path))


def test_text_auto_widens_finds_no_trace_startup_record(no_trace_startup_executor):
    """Bare text search finds a startup record that carries no trace field.

    This is the real bug shape: the agent asks "how many dimensions does graph
    memory have?" and log_query must find the startup INFO record even though
    the current contextvars trace is TRACE_A and the record has no trace key.
    Auto-widening (no explicit trace + text given) makes this work.
    """
    structlog.contextvars.bind_contextvars(trace=TRACE_A)
    try:
        payload = _query(no_trace_startup_executor, text="GraphMemoryStore")
    finally:
        structlog.contextvars.clear_contextvars()

    # The traceless startup record must be found despite a different active trace.
    assert payload["total_matched"] == 1
    assert payload["records"][0]["msg"].startswith("GraphMemoryStore")
    assert "dim=1536" in payload["records"][0]["msg"]


def test_text_explicit_trace_excludes_no_trace_record(no_trace_startup_executor):
    """An explicit trace scopes out records that carry no trace field."""
    # Explicitly searching TRACE_A should NOT return the traceless startup record.
    payload = _query(no_trace_startup_executor, trace=TRACE_A, text="GraphMemoryStore")
    assert payload["total_matched"] == 0


def test_text_null_trace_auto_widens(no_trace_startup_executor):
    """trace=None is treated as unset, so bare text search still auto-widens."""
    structlog.contextvars.bind_contextvars(trace=TRACE_A)
    try:
        payload = _query(no_trace_startup_executor, trace=None, text="GraphMemoryStore")
    finally:
        structlog.contextvars.clear_contextvars()

    assert payload["total_matched"] == 1
    assert "dim=1536" in payload["records"][0]["msg"]


# ---------------------------------------------------------------------------
# Unicode / casefold correctness
# ---------------------------------------------------------------------------

@pytest.fixture
def unicode_executor(tmp_path):
    """Log with a record whose msg contains non-ASCII characters (Straße, café)."""
    records = [
        {"ts": "2026-07-05T10:00:00", "trace": TRACE_A, "level": "info",
         "event_type": "STEP_BEGIN",
         "msg": "Straße café: vector store ready"},
    ]
    path = tmp_path / "unicode.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    return BuiltinExecutor(log_jsonl_path=str(path))


def test_text_search_casefold_unicode(unicode_executor):
    """casefold() matches German sharp-s and accented characters case-insensitively.

    'straße' casefolds to 'strasse'; 'STRASSE' also casefolds to 'strasse'.
    'CAFÉ' casefolds to 'café'. Both must match the stored record.
    """
    # German ß: "STRASSE" should match "Straße" via casefold (ß → ss).
    payload_ss = _query(unicode_executor, trace="*", text="STRASSE")
    assert payload_ss["total_matched"] == 1, "STRASSE should match Straße via casefold"

    # Accented character: "CAFÉ" should match "café".
    payload_cafe = _query(unicode_executor, trace="*", text="CAFÉ")
    assert payload_cafe["total_matched"] == 1, "CAFÉ should match café via casefold"

    # Lowercase variant also matches.
    payload_lower = _query(unicode_executor, trace="*", text="straße café")
    assert payload_lower["total_matched"] == 1

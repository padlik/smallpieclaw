"""Tests for the ``log_query`` built-in tool against the SQLite structured log store.

Covers trace filtering, the current-run-trace default, the Option C default
view (STEP_* excluded / TOOL_*/LLM_CALL & WARNING+ included), the explicit
level/event_type/tool/since filters, text search (including extra-field and
Unicode casefold matches), prompt_id filtering, limit/truncation with exact
``total_matched``, empty/missing-store handling, and the absence of the
legacy window-saturation fields.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
import structlog

from sqlite_log import SCHEMA_DDL, record_to_row

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
def executor(make_builtin_executor, tmp_path, sample_records):
    """Executor wired to a temp SQLite store containing *sample_records*."""
    path = str(tmp_path / "agent_logs.sqlite")
    _insert_records(path, sample_records)
    return make_builtin_executor(log_store_path=path)


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

    payload_warning = _query(executor, trace="*", level="WARNING")
    # WARNING includes the error-level TOOL_FAILED plus the warning STEP_END.
    assert payload_warning["total_matched"] == 2
    assert {"warning", "error"} == {r["level"] for r in payload_warning["records"]}


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
    # The most recent records are kept, then re-ascended (oldest-first in output).
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


def test_response_has_no_window_fields(executor):
    """The legacy window_saturated / scanned_lines disclosure fields are gone."""
    payload = _query(executor, trace="*", level="DEBUG")
    assert "window_saturated" not in payload
    assert "scanned_lines" not in payload


def test_missing_file_returns_empty(make_builtin_executor, tmp_path):
    exc = make_builtin_executor(log_store_path=str(tmp_path / "does_not_exist.sqlite"))
    result = exc.execute("log_query", {})
    assert result["success"] is True
    payload = json.loads(result["output"])
    assert payload["records"] == []
    assert payload["count"] == 0
    assert payload["truncated"] is False
    assert payload["total_matched"] == 0
    assert "window_saturated" not in payload
    assert "scanned_lines" not in payload


def test_unset_path_returns_empty(make_builtin_executor):
    exc = make_builtin_executor()  # log_store_path defaults to ""
    result = exc.execute("log_query", {})
    assert result["success"] is True
    payload = json.loads(result["output"])
    assert payload["records"] == []
    assert payload["total_matched"] == 0
    assert payload["truncated"] is False
    assert "window_saturated" not in payload
    assert "scanned_lines" not in payload


def test_empty_store_well_formed(make_builtin_executor, tmp_path):
    path = str(tmp_path / "empty.sqlite")
    _insert_records(path, [])  # creates schema, no rows
    exc = make_builtin_executor(log_store_path=path)
    payload = _query(exc)
    assert payload["records"] == []
    assert payload["count"] == 0
    assert payload["truncated"] is False
    assert payload["total_matched"] == 0


def test_bare_sqlite_file_no_events_table_returns_empty(make_builtin_executor, tmp_path):
    """A file present but with no events table must yield a well-formed empty result."""
    path = str(tmp_path / "no_events.sqlite")
    sqlite3.connect(path).close()
    exc = make_builtin_executor(log_store_path=path)
    result = exc.execute("log_query", {})
    assert result["success"] is True
    payload = json.loads(result["output"])
    assert payload["records"] == []
    assert payload["count"] == 0
    assert payload["truncated"] is False
    assert payload["total_matched"] == 0


def test_exact_total_matched_across_large_match_set(make_builtin_executor, tmp_path):
    """120 matching records, limit 50 -> count=50, truncated=True, total_matched=120."""
    records = [
        {"ts": f"2026-07-05T12:{i:02d}:00", "trace": TRACE_A, "level": "info",
         "event_type": "TOOL_END", "tool": "shell"}
        for i in range(120)
    ]
    path = str(tmp_path / "many.sqlite")
    _insert_records(path, records)
    exc = make_builtin_executor(log_store_path=path, max_output=50000)

    payload = _query(exc, trace=TRACE_A, event_type="TOOL_END", limit=50)
    assert payload["total_matched"] == 120
    assert payload["count"] == 50
    assert payload["truncated"] is True
    # Returned records are the most recent 50, re-ascended.  Because the
    # artificial timestamps use a fixed hour prefix, lexical sorting on the
    # formatted minute suffix returns minutes 50-99 (the last 50 inserted).
    assert [r["ts"] for r in payload["records"]] == [
        f"2026-07-05T12:{i:02d}:00" for i in range(50, 100)
    ]


def test_huge_limit_is_clamped_to_max(make_builtin_executor, tmp_path):
    """An LLM-reachable huge limit is clamped so the full store is not
    materialized; total_matched stays exact and truncation is reported."""
    records = [
        {"ts": f"2026-07-05T12:{i:03d}:00", "trace": TRACE_A, "level": "info",
         "event_type": "TOOL_END", "tool": "shell"}
        for i in range(550)
    ]
    path = str(tmp_path / "huge_limit.sqlite")
    _insert_records(path, records)
    exc = make_builtin_executor(log_store_path=path, max_output=100_000)

    payload = _query(exc, trace=TRACE_A, event_type="TOOL_END", limit=1_000_000)
    assert payload["total_matched"] == 550
    assert payload["count"] == 500
    assert payload["truncated"] is True


# ---------------------------------------------------------------------------
# text / query argument — full-record substring search
# ---------------------------------------------------------------------------

@pytest.fixture
def text_search_executor(make_builtin_executor, tmp_path):
    """Executor with records that span traces, levels, and a distinctive INFO
    startup message dropped by the Option C default view."""
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
        # Extra-field text match (err inside extra)
        {"ts": "2026-07-05T10:00:04", "trace": TRACE_A, "level": "error",
         "event_type": "TOOL_FAILED", "tool": "shell",
         "err": "network timeout exceeded"},
        # Literal % and _ characters (not LIKE wildcards)
        {"ts": "2026-07-05T10:00:05", "trace": TRACE_A, "level": "info",
         "event_type": "STEP_END", "msg": "100% complete _done"},
    ]
    path = str(tmp_path / "text.sqlite")
    _insert_records(path, records)
    return make_builtin_executor(log_store_path=path)


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


def test_text_search_matches_extra_field(text_search_executor):
    """The full-record search includes values stored in the extra JSON column."""
    payload = _query(text_search_executor, trace="*", text="timeout exceeded")
    assert payload["total_matched"] == 1
    assert payload["records"][0]["event_type"] == "TOOL_FAILED"
    assert "timeout exceeded" in payload["records"][0]["err"]


def test_query_alias_behaves_same(text_search_executor):
    """query= alias produces the same results as text=."""
    payload_text = _query(text_search_executor, trace="*", text="GraphMemoryStore")
    payload_query = _query(text_search_executor, trace="*", query="GraphMemoryStore")
    assert payload_text["total_matched"] == payload_query["total_matched"]
    assert payload_text["records"] == payload_query["records"]


def test_text_search_case_insensitive(text_search_executor):
    """text= search is Unicode-aware case-insensitive (casefold)."""
    payload_upper = _query(text_search_executor, trace="*", text="GRAPHMEMORYSTORE")
    payload_lower = _query(text_search_executor, trace="*", text="graphmemorystore")
    payload_mixed = _query(text_search_executor, trace="*", text="GraphMemoryStore")
    assert (
        payload_upper["total_matched"]
        == payload_lower["total_matched"]
        == payload_mixed["total_matched"]
    )
    assert payload_upper["records"] == payload_lower["records"] == payload_mixed["records"]
    assert payload_upper["total_matched"] == 1


def test_text_search_wildcard_chars_are_literal(text_search_executor):
    """% and _ in the needle are ordinary characters, not LIKE wildcards."""
    payload_pct = _query(text_search_executor, trace="*", text="100%")
    assert payload_pct["total_matched"] == 1
    assert "100%" in payload_pct["records"][0]["msg"]

    payload_underscore = _query(text_search_executor, trace="*", text="_done")
    assert payload_underscore["total_matched"] == 1
    assert "_done" in payload_underscore["records"][0]["msg"]

    # A LIKE wildcard should NOT match the literal text.
    payload_like = _query(text_search_executor, trace="*", text="100Xcomplete")
    assert payload_like["total_matched"] == 0


def test_text_search_auto_widens_without_explicit_trace(text_search_executor):
    """Without an explicit trace, a text search auto-widens to all traces."""
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
def no_trace_startup_executor(make_builtin_executor, tmp_path):
    """Log with a traceless startup record and a current-run record on TRACE_A."""
    records = [
        # Traceless startup record — emitted before trace ID is available.
        {"ts": "2026-07-05T09:59:58", "level": "info",
         "event_type": "RUN_BEGIN",
         "msg": "GraphMemoryStore initialised at data/graph_memory (dim=1536)"},
        # Same-run record on TRACE_A.
        {"ts": "2026-07-05T10:00:00", "trace": TRACE_A, "level": "info",
         "event_type": "TOOL_START", "tool": "shell"},
    ]
    path = str(tmp_path / "startup.sqlite")
    _insert_records(path, records)
    return make_builtin_executor(log_store_path=path)


def test_text_auto_widens_finds_no_trace_startup_record(no_trace_startup_executor):
    """Bare text search finds a startup record that carries no trace field."""
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
def unicode_executor(make_builtin_executor, tmp_path):
    """Log with a record whose msg contains non-ASCII characters (Straße, café)."""
    records = [
        {"ts": "2026-07-05T10:00:00", "trace": TRACE_A, "level": "info",
         "event_type": "STEP_BEGIN",
         "msg": "Straße café: vector store ready"},
    ]
    path = str(tmp_path / "unicode.sqlite")
    _insert_records(path, records)
    return make_builtin_executor(log_store_path=path)


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


# ---------------------------------------------------------------------------
# prompt_id filtering
# ---------------------------------------------------------------------------

@pytest.fixture
def prompt_id_executor(make_builtin_executor, tmp_path):
    """Log with records carrying prompt_id values plus one without."""
    records = [
        {"ts": "2026-07-05T10:00:00", "trace": TRACE_A, "level": "info",
         "event_type": "TOOL_START", "tool": "shell", "prompt_id": "01JARYN6R0ABCDEFGHJKMNPQRS"},
        {"ts": "2026-07-05T10:00:01", "trace": TRACE_A, "level": "info",
         "event_type": "TOOL_END", "tool": "shell", "prompt_id": "01JARYN6R0ABCDEFGHJKMNPQRS"},
        {"ts": "2026-07-05T10:00:02", "trace": TRACE_A, "level": "info",
         "event_type": "LLM_CALL", "prompt_id": "01JARYZ3W2ABCDEFGHJKMNPQRS"},
        {"ts": "2026-07-05T10:00:03", "trace": TRACE_A, "level": "info",
         "event_type": "TOOL_START", "tool": "schedule"},
    ]
    path = str(tmp_path / "prompt_id.sqlite")
    _insert_records(path, records)
    return make_builtin_executor(log_store_path=path)


def test_prompt_id_filter_returns_only_matches(prompt_id_executor):
    payload = _query(prompt_id_executor, trace="*", prompt_id="01JARYN6R0ABCDEFGHJKMNPQRS")
    assert payload["total_matched"] == 2
    assert {r["tool"] for r in payload["records"]} == {"shell"}


def test_prompt_id_filter_excludes_other(prompt_id_executor):
    payload = _query(prompt_id_executor, trace="*", prompt_id="01JARYZ3W2ABCDEFGHJKMNPQRS")
    assert payload["total_matched"] == 1
    assert payload["records"][0]["event_type"] == "LLM_CALL"


def test_prompt_id_filter_no_match(prompt_id_executor):
    payload = _query(prompt_id_executor, trace="*", prompt_id="01JNONEXISTENTULIDSTRING00000")
    assert payload["total_matched"] == 0
    assert payload["records"] == []


def test_prompt_id_auto_widens_to_all_traces(prompt_id_executor):
    """prompt_id without explicit trace auto-widens so cross-trace matches are found."""
    structlog.contextvars.bind_contextvars(trace=TRACE_B)
    try:
        payload = _query(prompt_id_executor, prompt_id="01JARYN6R0ABCDEFGHJKMNPQRS")
    finally:
        structlog.contextvars.clear_contextvars()
    assert payload["total_matched"] == 2


def test_prompt_id_explicit_trace_overrides_widening(make_builtin_executor, tmp_path):
    """Explicit trace restricts prompt_id matches to that trace."""
    records = [
        {"ts": "2026-07-05T10:00:00", "trace": TRACE_A, "level": "info",
         "event_type": "TOOL_START", "prompt_id": "01JARYN6R0ABCDEFGHJKMNPQRS"},
        {"ts": "2026-07-05T10:00:01", "trace": TRACE_B, "level": "info",
         "event_type": "TOOL_START", "prompt_id": "01JARYN6R0ABCDEFGHJKMNPQRS"},
    ]
    path = str(tmp_path / "prompt_trace.sqlite")
    _insert_records(path, records)
    exc = make_builtin_executor(log_store_path=path)
    payload = _query(exc, trace=TRACE_B, prompt_id="01JARYN6R0ABCDEFGHJKMNPQRS")
    assert payload["total_matched"] == 1
    assert payload["records"][0]["trace"] == TRACE_B


# ---------------------------------------------------------------------------
# max_output size cap
# ---------------------------------------------------------------------------

def test_max_output_size_cap(make_builtin_executor, tmp_path):
    """A small max_output truncates the returned records to fit the budget."""
    records = [
        {"ts": "2026-07-05T10:00:00", "trace": TRACE_A, "level": "info",
         "event_type": "TOOL_START", "msg": "short a"},
        {"ts": "2026-07-05T10:00:01", "trace": TRACE_A, "level": "info",
         "event_type": "TOOL_START", "msg": "x" * 500},
    ]
    path = str(tmp_path / "cap.sqlite")
    _insert_records(path, records)
    exc = make_builtin_executor(log_store_path=path, max_output=120)
    payload = _query(exc, trace="*", level="DEBUG")
    # The newest record is always kept even if it alone exceeds the budget.
    assert payload["count"] >= 1
    assert payload["truncated"] is True
    assert payload["total_matched"] == 2

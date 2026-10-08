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
    # The page query is always ordered by id (newest id first, re-ascended for
    # output), so the 50 returned records are those with the highest ids.
    returned_ids = [r["id"] for r in payload["records"]]
    assert returned_ids == list(range(71, 121))
    assert [r["ts"] for r in payload["records"]] == [
        f"2026-07-05T12:{i:02d}:00" for i in range(70, 120)
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


# ---------------------------------------------------------------------------
# id exposure and before_id paging (spec 5.1-5.4)
# ---------------------------------------------------------------------------

def _insert_records_with_id(path: str, records: list[dict]) -> None:
    """Create schema and insert records with explicit integer ids.

    The id is prepended to the row tuple produced by ``record_to_row``.
    """
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_DDL)
    if records:
        for rec in records:
            rec_id = rec["id"]
            base_row = record_to_row({k: v for k, v in rec.items() if k != "id"})
            row = (rec_id,) + base_row
            conn.execute(
                """
                INSERT INTO events
                (id, ts, level, logger, agent, trace, prompt_id, event_type, msg, extra, search_text)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )
    conn.commit()
    conn.close()


def test_every_record_carries_store_id(make_builtin_executor, tmp_path):
    """Every log_query record includes its immutable SQLite primary key."""
    records = [
        {"id": 10, "ts": "2026-07-05T10:00:00", "trace": TRACE_A, "level": "info",
         "event_type": "TOOL_START"},
        {"id": 20, "ts": "2026-07-05T10:00:01", "trace": TRACE_A, "level": "info",
         "event_type": "TOOL_END"},
        {"id": 30, "ts": "2026-07-05T10:00:02", "trace": TRACE_A, "level": "info",
         "event_type": "LLM_CALL"},
    ]
    path = str(tmp_path / "ids.sqlite")
    _insert_records_with_id(path, records)
    exc = make_builtin_executor(log_store_path=path)

    payload = _query(exc, trace=TRACE_A, level="DEBUG")
    returned_ids = [r["id"] for r in payload["records"]]
    assert returned_ids == [10, 20, 30]
    # Direct SQLite cross-check.
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute("SELECT id FROM events ORDER BY id")
        db_ids = [row["id"] for row in cur.fetchall()]
    finally:
        conn.close()
    assert returned_ids == db_ids


def test_before_id_pages_newest_to_oldest_gapless(make_builtin_executor, tmp_path):
    """120 records ids 801..920: two pages, no overlap, total_matched constant."""
    records = [
        {"id": 800 + i, "ts": f"2026-07-05T12:{i:02d}:00", "trace": TRACE_A,
         "level": "info", "event_type": "TOOL_END", "tool": "shell"}
        for i in range(1, 121)
    ]
    path = str(tmp_path / "paging.sqlite")
    _insert_records_with_id(path, records)
    exc = make_builtin_executor(log_store_path=path, max_output=1_000_000)

    # Page 1: newest 50 ids (920 down to 871), re-ascended for output.
    p1 = _query(exc, trace=TRACE_A, event_type="TOOL_END", limit=50)
    ids1 = [r["id"] for r in p1["records"]]
    assert ids1 == list(range(871, 921))
    assert p1["total_matched"] == 120
    assert p1["truncated"] is True

    # Page 2: anchored on the smallest id seen.
    before = min(ids1)
    p2 = _query(exc, trace=TRACE_A, event_type="TOOL_END", limit=50, before_id=before)
    ids2 = [r["id"] for r in p2["records"]]
    assert ids2 == list(range(821, 871))
    assert p2["total_matched"] == 120
    assert p2["truncated"] is True
    assert not set(ids1) & set(ids2)

    # Walk to exhaustion.
    all_ids = set(ids1) | set(ids2)
    while True:
        page_ids = [r["id"] for r in p2["records"]]
        if not page_ids:
            break
        before = min(page_ids)
        p2 = _query(exc, trace=TRACE_A, event_type="TOOL_END", limit=50, before_id=before)
        new_ids = [r["id"] for r in p2["records"]]
        assert not all_ids & set(new_ids), "walk produced duplicate ids"
        all_ids.update(new_ids)

    assert sorted(all_ids) == list(range(801, 921))


def test_before_id_walk_while_appending(make_builtin_executor, tmp_path):
    """Live appends between pages: no duplicates, total_matched grows, ends on empty page."""
    records = [
        {"id": i, "ts": f"2026-07-05T12:{i:02d}:00", "trace": TRACE_A,
         "level": "info", "event_type": "TOOL_END", "tool": "shell"}
        for i in range(1, 61)
    ]
    path = str(tmp_path / "live.sqlite")
    _insert_records_with_id(path, records)
    exc = make_builtin_executor(log_store_path=path, max_output=1_000_000)

    p1 = _query(exc, trace=TRACE_A, event_type="TOOL_END", limit=25)
    ids1 = [r["id"] for r in p1["records"]]
    assert p1["total_matched"] == 60

    # Append new matching rows with higher ids between pages.
    conn = sqlite3.connect(path)
    try:
        for i in range(61, 81):
            rec = {"id": i, "ts": f"2026-07-05T12:{i:02d}:00", "trace": TRACE_A,
                   "level": "info", "event_type": "TOOL_END", "tool": "shell"}
            base_row = record_to_row({k: v for k, v in rec.items() if k != "id"})
            row = (i,) + base_row
            conn.execute(
                """
                INSERT INTO events
                (id, ts, level, logger, agent, trace, prompt_id, event_type, msg, extra, search_text)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )
        conn.commit()
    finally:
        conn.close()

    before = min(ids1)
    p2 = _query(exc, trace=TRACE_A, event_type="TOOL_END", limit=25, before_id=before)
    ids2 = [r["id"] for r in p2["records"]]
    assert p2["total_matched"] == 80, "total_matched must grow after live appends"
    assert not set(ids1) & set(ids2), "page ids must never repeat"

    all_ids = set(ids1) | set(ids2)
    current = p2
    while True:
        page_ids = [r["id"] for r in current["records"]]
        if not page_ids:
            break
        before = min(page_ids)
        current = _query(
            exc, trace=TRACE_A, event_type="TOOL_END", limit=25, before_id=before
        )
        new_ids = [r["id"] for r in current["records"]]
        assert not all_ids & set(new_ids)
        all_ids.update(new_ids)

    # The walk covers the original 60 records exactly; new appends have higher
    # ids than every page anchor and are correctly outside the walk.
    assert sorted(all_ids) == list(range(1, 61))


def test_before_id_gapless_under_ts_id_inversion_count_truncated(
    make_builtin_executor, tmp_path,
):
    """Older-ts records have higher ids; count-truncated walk is still gapless."""
    # Newest ts first, older ts second → ids increase opposite to ts order.
    records = [
        {"id": 1, "ts": "2026-07-05T10:00:06", "trace": TRACE_A, "level": "info",
         "event_type": "TOOL_FAILED", "err": "newest"},
        {"id": 2, "ts": "2026-07-05T10:00:05", "trace": TRACE_A, "level": "info",
         "event_type": "TOOL_FAILED", "err": "old"},
        {"id": 3, "ts": "2026-07-05T10:00:04", "trace": TRACE_A, "level": "info",
         "event_type": "TOOL_FAILED", "err": "older"},
        {"id": 4, "ts": "2026-07-05T10:00:03", "trace": TRACE_A, "level": "info",
         "event_type": "TOOL_FAILED", "err": "oldest"},
        {"id": 5, "ts": "2026-07-05T10:00:02", "trace": TRACE_A, "level": "info",
         "event_type": "TOOL_FAILED", "err": "very oldest"},
        {"id": 6, "ts": "2026-07-05T10:00:01", "trace": TRACE_A, "level": "info",
         "event_type": "TOOL_FAILED", "err": "ancient"},
    ]
    path = str(tmp_path / "inversion.sqlite")
    _insert_records_with_id(path, records)
    exc = make_builtin_executor(log_store_path=path, max_output=10_000)

    all_ids: set[int] = set()
    before = None
    for _ in range(5):
        args = {"trace": TRACE_A, "event_type": "TOOL_FAILED", "limit": 3}
        if before is not None:
            args["before_id"] = before
        payload = _query(exc, **args)
        page_ids = [r["id"] for r in payload["records"]]
        if not page_ids:
            break
        assert not all_ids & set(page_ids)
        all_ids.update(page_ids)
        before = min(page_ids)

    assert sorted(all_ids) == [1, 2, 3, 4, 5, 6]


def test_before_id_gapless_under_ts_id_inversion_size_trimmed(
    make_builtin_executor, tmp_path,
):
    """Inversion + max_output trim: kept ids are the highest, walk stays gapless."""
    records = [
        {"id": 1, "ts": "2026-07-05T10:00:04", "trace": TRACE_A, "level": "info",
         "event_type": "TOOL_FAILED", "err": "x" * 2000},
        {"id": 2, "ts": "2026-07-05T10:00:03", "trace": TRACE_A, "level": "info",
         "event_type": "TOOL_FAILED", "err": "x" * 2000},
        {"id": 3, "ts": "2026-07-05T10:00:02", "trace": TRACE_A, "level": "info",
         "event_type": "TOOL_FAILED", "err": "x" * 2000},
        {"id": 4, "ts": "2026-07-05T10:00:01", "trace": TRACE_A, "level": "info",
         "event_type": "TOOL_FAILED", "err": "x" * 2000},
    ]
    path = str(tmp_path / "inversion_size.sqlite")
    _insert_records_with_id(path, records)
    exc = make_builtin_executor(log_store_path=path, max_output=120)

    # First page: should be trimmed to the highest ids only.
    p1 = _query(exc, trace=TRACE_A, event_type="TOOL_FAILED", limit=50)
    kept_ids = [r["id"] for r in p1["records"]]
    assert p1["truncated"] is True
    assert kept_ids == sorted(kept_ids), "output is re-ascended"
    assert 4 in kept_ids, "size-trim must keep the highest ids"

    all_ids = set(kept_ids)
    before = min(kept_ids)
    while True:
        p = _query(exc, trace=TRACE_A, event_type="TOOL_FAILED", limit=50, before_id=before)
        page_ids = [r["id"] for r in p["records"]]
        if not page_ids:
            break
        assert not all_ids & set(page_ids)
        all_ids.update(page_ids)
        before = min(page_ids)

    assert sorted(all_ids) == [1, 2, 3, 4]


def test_before_id_composes_with_other_filters(make_builtin_executor, tmp_path):
    """before_id works with event_type, prompt_id, and text filters."""
    records = [
        {"id": 10, "ts": "2026-07-05T10:00:00", "trace": TRACE_A, "level": "info",
         "event_type": "TOOL_FAILED", "tool": "shell", "prompt_id": "01PROMPTAAAA",
         "err": "alpha timeout"},
        {"id": 20, "ts": "2026-07-05T10:00:01", "trace": TRACE_A, "level": "info",
         "event_type": "TOOL_END", "tool": "shell", "prompt_id": "01PROMPTAAAA"},
        {"id": 30, "ts": "2026-07-05T10:00:02", "trace": TRACE_A, "level": "info",
         "event_type": "TOOL_FAILED", "tool": "file_read", "prompt_id": "01PROMPTBBBB",
         "err": "beta timeout"},
        {"id": 40, "ts": "2026-07-05T10:00:03", "trace": TRACE_A, "level": "info",
         "event_type": "TOOL_FAILED", "tool": "shell", "prompt_id": "01PROMPTAAAA",
         "err": "gamma timeout"},
    ]
    path = str(tmp_path / "compose.sqlite")
    _insert_records_with_id(path, records)
    exc = make_builtin_executor(log_store_path=path)

    # before_id + event_type.
    p = _query(exc, trace=TRACE_A, event_type="TOOL_FAILED", before_id=35)
    assert [r["id"] for r in p["records"]] == [10, 30]

    # before_id + prompt_id.
    p = _query(exc, trace=TRACE_A, prompt_id="01PROMPTAAAA", before_id=40)
    assert [r["id"] for r in p["records"]] == [10, 20]

    # before_id + text.
    p = _query(exc, trace=TRACE_A, text="timeout", before_id=40)
    assert [r["id"] for r in p["records"]] == [10, 30]


def test_invalid_before_id_omits_filter_returns_newest_page(make_builtin_executor, tmp_path):
    """null, non-int, <=0, and out-of-range before_id values are ignored.

    ``float("inf")`` pins the coercion-overflow boundary: JSON ``1e999``
    parses to inf, and ``int(inf)`` raises OverflowError — the handler must
    degrade to the newest page, never raise.
    """
    records = [
        {"id": i, "ts": f"2026-07-05T10:00:{i:02d}", "trace": TRACE_A,
         "level": "info", "event_type": "TOOL_START"}
        for i in range(1, 11)
    ]
    path = str(tmp_path / "invalid.sqlite")
    _insert_records_with_id(path, records)
    exc = make_builtin_executor(log_store_path=path)

    baseline = _query(exc, trace=TRACE_A, event_type="TOOL_START", limit=5)
    baseline_ids = [r["id"] for r in baseline["records"]]

    for bad in (None, "abc", 0, -5, 2**63, float("inf"), float("nan")):
        payload = _query(
            exc, trace=TRACE_A, event_type="TOOL_START", limit=5, before_id=bad
        )
        assert [r["id"] for r in payload["records"]] == baseline_ids


def test_infinite_limit_falls_back_to_default(make_builtin_executor, tmp_path):
    """A limit of inf (JSON 1e999) degrades to the default, never raises."""
    records = [
        {"id": i, "ts": f"2026-07-05T10:00:{i:02d}", "trace": TRACE_A,
         "level": "info", "event_type": "TOOL_START"}
        for i in range(1, 11)
    ]
    path = str(tmp_path / "inf_limit.sqlite")
    _insert_records_with_id(path, records)
    exc = make_builtin_executor(log_store_path=path)

    payload = _query(exc, trace=TRACE_A, event_type="TOOL_START",
                     limit=float("inf"))
    assert payload["count"] == 10  # default limit 50 covers the whole set


def test_all_result_pages_are_id_ordered(make_builtin_executor, tmp_path):
    """Every returned page is id-ascending in output (always-id-DESC rule)."""
    records = [
        {"id": i, "ts": f"2026-07-05T10:00:{i:02d}", "trace": TRACE_A,
         "level": "info", "event_type": "TOOL_START"}
        for i in range(1, 21)
    ]
    path = str(tmp_path / "ordered.sqlite")
    _insert_records_with_id(path, records)
    exc = make_builtin_executor(log_store_path=path)

    p1 = _query(exc, trace=TRACE_A, event_type="TOOL_START", limit=7)
    assert [r["id"] for r in p1["records"]] == sorted(r["id"] for r in p1["records"])

    before = min(r["id"] for r in p1["records"])
    p2 = _query(exc, trace=TRACE_A, event_type="TOOL_START", limit=7, before_id=before)
    assert [r["id"] for r in p2["records"]] == sorted(r["id"] for r in p2["records"])



# ---------------------------------------------------------------------------
# Read-time redaction of the text= search corpus (no credential oracle)
# ---------------------------------------------------------------------------

_HEX = "0123456789abcdef"


def _redacting_executor(make_builtin_executor, tmp_path, records, secrets_toml):
    from config_schema import ExecutorPaths

    path = str(tmp_path / "oracle.sqlite")
    _insert_records(path, records)
    vault = tmp_path / "vault.toml"
    vault.write_text(secrets_toml)
    return make_builtin_executor(
        paths=ExecutorPaths(log_store_path=path, vault_path=str(vault)),
        max_output=1_000_000,
    )


def test_adaptive_prefix_attack_cannot_recover_credential(make_builtin_executor, tmp_path):
    """Extending a text= prefix never matches a credential added to the vault after ingestion."""
    secret = "ab12ba21"
    records = [
        {"ts": "2026-07-05T10:00:00", "trace": TRACE_A, "level": "error",
         "msg": f"login failed token={secret}"},
        {"ts": "2026-07-05T10:00:01", "trace": TRACE_A, "level": "error",
         "msg": "login failed token=unrelated"},
    ]
    exc = _redacting_executor(
        make_builtin_executor, tmp_path, records, f'late_key = "{secret}"\n'
    )

    recovered = ""
    for _ in range(len(secret)):
        hits = [
            c for c in _HEX
            if _query(exc, text=f"token={recovered}{c}")["total_matched"] > 0
        ]
        if len(hits) != 1:
            break
        recovered += hits[0]

    assert recovered == ""
    for end in range(1, len(secret) + 1):
        payload = _query(exc, text=f"token={secret[:end]}")
        assert payload["total_matched"] == 0
        assert payload["count"] == 0


def test_redacted_corpus_still_matches_surrounding_text(make_builtin_executor, tmp_path):
    secret = "ab12ba21"
    records = [{"ts": "2026-07-05T10:00:00", "trace": TRACE_A, "level": "error",
                "msg": f"login failed token={secret}"}]
    exc = _redacting_executor(
        make_builtin_executor, tmp_path, records, f'late_key = "{secret}"\n'
    )

    payload = _query(exc, text="login failed token=[REDACTED]")

    assert payload["total_matched"] == 1
    assert secret not in json.dumps(payload)


def test_text_count_and_paging_are_exact_on_redacted_corpus(make_builtin_executor, tmp_path):
    secret = "boomsecret99"
    records = []
    for i in range(6):
        records.append({"ts": f"2026-07-05T10:00:{i:02d}", "trace": TRACE_A,
                        "level": "info", "msg": f"boom clean {i}"})
        records.append({"ts": f"2026-07-05T11:00:{i:02d}", "trace": TRACE_A,
                        "level": "info", "msg": f"boom dirty {i} {secret}"})
    records.append({"ts": "2026-07-05T12:00:00", "trace": TRACE_A, "level": "info",
                    "msg": f"only the secret {secret}"})
    exc = _redacting_executor(
        make_builtin_executor, tmp_path, records, f'k = "{secret}"\n'
    )

    first = _query(exc, text="boom", limit=5)
    ids = [r["id"] for r in first["records"]]
    pages = [ids]
    before = min(ids)
    while True:
        page = _query(exc, text="boom", limit=5, before_id=before)
        assert page["total_matched"] == first["total_matched"]
        if not page["records"]:
            break
        pages.append([r["id"] for r in page["records"]])
        before = min(pages[-1])

    walked = [i for p in pages for i in p]
    assert first["total_matched"] == 12
    assert len(walked) == len(set(walked)) == 12
    assert _query(exc, text="boomsecret99")["total_matched"] == 0


def test_text_search_without_vault_secrets_is_unchanged(make_builtin_executor, tmp_path):
    records = [{"ts": "2026-07-05T10:00:00", "trace": TRACE_A, "level": "info",
                "msg": "boom visible"}]
    exc = _redacting_executor(make_builtin_executor, tmp_path, records, 'k = "zzzzzzzz"\n')

    assert _query(exc, text="boom visible")["total_matched"] == 1


# ---------------------------------------------------------------------------
# Nested JSON serialization of a credential (repeated escaping) and bounded
# retention of dirty matches
# ---------------------------------------------------------------------------

_ESCAPE_SECRET = 'ab12"ba21'
_ESCAPE_VAULT = f"late_key = '{_ESCAPE_SECRET}'\n"


def _nest(text: str, layers: int) -> str:
    for _ in range(layers):
        text = json.dumps(text)
    return text


def _decode_levels(text: str):
    """Yield *text* and every successive json.loads() layer that stays a string."""
    for _ in range(16):
        yield text
        try:
            decoded = json.loads(text)
        except ValueError:
            return
        if not isinstance(decoded, str):
            return
        text = decoded


def _nested_records(layers: int) -> list[dict]:
    return [
        {"ts": "2026-07-05T10:00:00", "trace": TRACE_A, "level": "error",
         "msg": _nest(f"token={_ESCAPE_SECRET}", layers)},
        {"ts": "2026-07-05T10:00:01", "trace": TRACE_A, "level": "error",
         "msg": "token=unrelated"},
    ]


@pytest.mark.parametrize(
    ("layers", "expected_matches"),
    [(1, 2), (2, 2), (3, 2), (4, 2), (6, 2), (7, 1)],
)
def test_nested_json_credential_not_recoverable_at_any_decode_depth(
    make_builtin_executor, tmp_path, layers, expected_matches
):
    exc = _redacting_executor(
        make_builtin_executor, tmp_path, _nested_records(layers), _ESCAPE_VAULT
    )

    payload = _query(exc, text="token=")

    assert payload["total_matched"] == expected_matches
    for record in payload["records"]:
        for level in _decode_levels(record["msg"]):
            assert _ESCAPE_SECRET not in level


@pytest.mark.parametrize("layers", [2, 3, 7])
def test_nested_json_prefix_probes_match_fully_redacted_corpus(
    make_builtin_executor, tmp_path, layers
):
    real = _redacting_executor(
        make_builtin_executor, tmp_path, _nested_records(layers), _ESCAPE_VAULT
    )
    scrubbed_records = _nested_records(layers)
    scrubbed_records[0]["msg"] = (
        _nest("token=[REDACTED]", layers) if layers <= 6 else "[REDACTED]"
    )
    scrubbed_dir = tmp_path / "scrubbed"
    scrubbed_dir.mkdir()
    scrubbed = _redacting_executor(
        make_builtin_executor, scrubbed_dir, scrubbed_records, 'k = "zzzzzzzz"\n'
    )

    probes = [f"token={_ESCAPE_SECRET[:end]}" for end in range(1, len(_ESCAPE_SECRET) + 1)]
    probes += [f'token={json.dumps(_ESCAPE_SECRET)[1:end + 1]}' for end in range(1, 8)]
    for probe in probes:
        got = _query(real, text=probe)
        expected = _query(scrubbed, text=probe)
        assert got["total_matched"] == expected["total_matched"] == 0, probe
        assert got["count"] == expected["count"], probe


def test_text_scan_retains_only_newest_limit_records():
    from builtin_tools.secrets_log import _scan_text_matches
    from sqlite_log import row_to_record  # noqa: F401  (rows built below)

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_DDL)
    for i in range(1, 201):
        conn.execute(
            "INSERT INTO events (id, ts, level, logger, agent, trace, prompt_id, event_type,"
            " msg, extra, search_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (i, *record_to_row(
                {"ts": f"2026-07-05T10:00:{i % 60:02d}", "level": "info",
                 "msg": f"boom {i} sekret-value"}
            )),
        )
    rows = conn.execute("SELECT * FROM events ORDER BY id DESC")

    count, retained = _scan_text_matches(rows, ["sekret-value"], "boom", None, 5)

    assert count == 200
    assert [rid for rid, _rec in retained] == [200, 199, 198, 197, 196]


def test_text_scan_before_id_counts_all_but_retains_only_eligible():
    from builtin_tools.secrets_log import _scan_text_matches

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_DDL)
    for i in range(1, 51):
        conn.execute(
            "INSERT INTO events (id, ts, level, logger, agent, trace, prompt_id, event_type,"
            " msg, extra, search_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (i, *record_to_row(
                {"ts": "2026-07-05T10:00:00", "level": "info", "msg": f"boom {i} sekret-value"}
            )),
        )
    rows = conn.execute("SELECT * FROM events ORDER BY id DESC")

    count, retained = _scan_text_matches(rows, ["sekret-value"], "boom", 20, 3)

    assert count == 50
    assert [rid for rid, _rec in retained] == [19, 18, 17]


def test_handler_bounds_retained_dirty_records_by_limit(
    make_builtin_executor, tmp_path, monkeypatch
):
    from builtin_tools import secrets_log

    secret = "sekret-value"
    records = [
        {"ts": f"2026-07-05T10:{i // 60:02d}:{i % 60:02d}", "trace": TRACE_A, "level": "info",
         "msg": f"boom {i} {secret}"}
        for i in range(120)
    ]
    exc = _redacting_executor(make_builtin_executor, tmp_path, records, f'k = "{secret}"\n')
    retained_sizes: list[int] = []
    real_scan = secrets_log._scan_text_matches

    def spy(*args, **kwargs):
        count, retained = real_scan(*args, **kwargs)
        retained_sizes.append(len(retained))
        return count, retained

    monkeypatch.setattr(secrets_log, "_scan_text_matches", spy)

    payload = _query(exc, text="boom", limit=4)

    assert payload["total_matched"] == 120
    assert payload["count"] == 4
    assert retained_sizes == [4]


# ---------------------------------------------------------------------------
# Unicode-escaped JSON spellings and many escape-heavy secrets
# ---------------------------------------------------------------------------


def _unicode_nest(text: str, layers: int) -> str:
    """Nest *text* in JSON string layers spelling quote/backslash as \\uXXXX."""
    for _ in range(layers):
        text = '"' + text.replace("\\", "\\u005c").replace('"', "\\u0022") + '"'
    return text


def _unicode_records(layers: int) -> list[dict]:
    records = _nested_records(layers)
    records[0]["msg"] = _unicode_nest(f"token={_ESCAPE_SECRET}", layers)
    return records


@pytest.mark.parametrize("layers", [1, 2, 3, 7, 8])
def test_unicode_escaped_credential_not_recoverable_at_any_decode_depth(
    make_builtin_executor, tmp_path, layers
):
    exc = _redacting_executor(
        make_builtin_executor, tmp_path, _unicode_records(layers), _ESCAPE_VAULT
    )

    payload = _query(exc, text="token=")

    assert payload["total_matched"] == 1
    for record in payload["records"]:
        for level in _decode_levels(record["msg"]):
            assert _ESCAPE_SECRET not in level


@pytest.mark.parametrize("layers", [1, 2, 3, 7, 8])
def test_unicode_escaped_prefix_probes_match_fully_redacted_corpus(
    make_builtin_executor, tmp_path, layers
):
    real = _redacting_executor(
        make_builtin_executor, tmp_path, _unicode_records(layers), _ESCAPE_VAULT
    )
    scrubbed_records = _unicode_records(layers)
    scrubbed_records[0]["msg"] = "[REDACTED]"
    scrubbed_dir = tmp_path / "scrubbed"
    scrubbed_dir.mkdir()
    scrubbed = _redacting_executor(
        make_builtin_executor, scrubbed_dir, scrubbed_records, 'k = "zzzzzzzz"\n'
    )

    probes = [f"token={_ESCAPE_SECRET[:end]}" for end in range(1, len(_ESCAPE_SECRET) + 1)]
    probes += ["token=a", "token=ab12", 'token=ab12\\u0022', "[REDACTED]"]
    for probe in probes:
        got = _query(real, text=probe)
        expected = _query(scrubbed, text=probe)
        assert got["total_matched"] == expected["total_matched"], probe
        assert got["count"] == expected["count"], probe


def test_many_escape_heavy_secrets_keep_text_search_working(
    make_builtin_executor, tmp_path
):
    secrets = [f'k{i}"\\é{i}x\ty' for i in range(30)]
    vault = "".join(
        f's{i} = "{json.dumps(s)[1:-1]}"\n' for i, s in enumerate(secrets)
    )
    records = [
        {"ts": f"2026-07-05T10:00:{i:02d}", "trace": TRACE_A, "level": "info",
         "msg": f"boom {i} {secret}"}
        for i, secret in enumerate(secrets)
    ]
    records += [
        {"ts": f"2026-07-05T11:00:{i:02d}", "trace": TRACE_A, "level": "info",
         "msg": f"boom clean {i}"}
        for i in range(5)
    ]
    exc = _redacting_executor(make_builtin_executor, tmp_path, records, vault)

    payload = _query(exc, text="boom", limit=100)

    assert payload["total_matched"] == 35
    assert payload["count"] == 35
    for secret in secrets:
        assert secret not in json.dumps(payload, ensure_ascii=False)


def test_text_search_builds_redaction_needles_once_per_query(
    make_builtin_executor, tmp_path, monkeypatch
):
    from builtin_tools import text_utils

    secrets = [f'k{i}"\\x{i}yz' for i in range(8)]
    vault = "".join(f's{i} = {json.dumps(s)}\n' for i, s in enumerate(secrets))
    records = [
        {"ts": f"2026-07-05T10:00:{i:02d}", "trace": TRACE_A, "level": "info",
         "msg": f"boom {i}"}
        for i in range(40)
    ]
    exc = _redacting_executor(make_builtin_executor, tmp_path, records, vault)
    text_utils._build_needles.cache_clear()
    calls = []
    real = text_utils._escape_forms
    monkeypatch.setattr(
        text_utils, "_escape_forms", lambda *a: calls.append(a) or real(*a)
    )

    assert _query(exc, text="boom")["total_matched"] == 40
    assert len(calls) <= len(secrets)

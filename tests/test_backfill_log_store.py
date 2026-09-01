"""Tests for the JSONL → SQLite structured-log backfill CLI.

Covers:
- Import from plain ``.jsonl`` and gzip ``.jsonl.*.gz`` archives.
- Wide-column and ``extra`` JSON mapping via :func:`sqlite_log.record_to_row`.
- Malformed/blank line skip counting.
- Idempotent re-run (bookkeeping table prevents duplicates).
- Source files remain untouched (read-only access).
- Imported rows are text-searchable identically to live rows.
- Dry-run writes nothing.
- CLI argument resolution via XDG paths and explicit overrides.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from pathlib import Path

import pytest  # noqa: F401

import backfill_log_store
from sqlite_log import record_to_row


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_record(**kwargs: object) -> dict:
    """Return a minimal valid structured-log record dict."""
    record = {
        "ts": "2026-07-01T12:00:00+00:00",
        "level": "info",
        "logger": "agent",
        "agent": "main",
        "trace": "r-00000001",
        "event_type": "TOOL_END",
        "msg": "tool finished",
    }
    record.update(kwargs)
    return record


def _write_jsonl(path: Path, records: list[dict]) -> None:
    """Write one JSON record per line to *path*."""
    with open(path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def _write_gz_jsonl(path: Path, records: list[dict]) -> None:
    """Write gzip-compressed JSONL records to *path*."""
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def _row_count(db_path: str) -> int:
    """Return the number of rows in the events table."""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute("SELECT COUNT(*) FROM events")
        return cur.fetchone()[0]
    finally:
        conn.close()


def _read_rows(db_path: str) -> list[sqlite3.Row]:
    """Return all events rows ordered by id."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute("SELECT * FROM events ORDER BY id")
        return cur.fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Import from .jsonl and .gz
# ---------------------------------------------------------------------------
class TestBackfillImportSources:
    def test_imports_plain_jsonl_and_gz(self, tmp_path):
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        db_path = tmp_path / "store.sqlite"

        plain_records = [
            _make_record(event_type="TOOL_START", tool="shell", msg="started"),
            _make_record(event_type="TOOL_END", tool="shell", dur_ms=42, msg="ended"),
        ]
        gz_records = [
            _make_record(event_type="LLM_CALL", model="gpt-4o-mini", msg="calling llm"),
        ]

        _write_jsonl(logs_dir / "agent.jsonl", plain_records)
        _write_gz_jsonl(logs_dir / "agent.jsonl.2026-07-02.gz", gz_records)

        result = backfill_log_store.backfill(
            str(logs_dir), str(db_path), dry_run=False, verbose=False
        )

        assert result.files_total == 2
        assert result.files_imported == 2
        assert result.records_imported == 3
        assert result.records_skipped == 0

        rows = _read_rows(str(db_path))
        assert len(rows) == 3

        # Wide columns
        assert rows[0]["event_type"] == "TOOL_START"
        assert rows[0]["agent"] == "main"
        assert rows[1]["event_type"] == "TOOL_END"
        assert rows[2]["event_type"] == "LLM_CALL"

        # Extra JSON preserved (tool, dur_ms, model)
        extras = [json.loads(row["extra"]) for row in rows]
        assert extras[0]["tool"] == "shell"
        assert extras[1]["tool"] == "shell"
        assert extras[1]["dur_ms"] == 42
        assert extras[2]["model"] == "gpt-4o-mini"

    def test_search_text_matches_record_to_row(self, tmp_path):
        """Imported rows must use the same search_text as a live-mapped row."""
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        db_path = tmp_path / "store.sqlite"

        record = _make_record(tool="shell", dur_ms=42, extra_field="value")
        _write_jsonl(logs_dir / "agent.jsonl", [record])

        backfill_log_store.backfill(str(logs_dir), str(db_path))

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT search_text FROM events").fetchone()
            expected = record_to_row(record)[-1]
            assert row["search_text"] == expected
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Malformed / blank line handling
# ---------------------------------------------------------------------------
class TestBackfillMalformedLines:
    def test_skips_malformed_and_blank_lines(self, tmp_path):
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        db_path = tmp_path / "store.sqlite"

        path = logs_dir / "agent.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(_make_record(event_type="TOOL_START")) + "\n")
            f.write("\n")
            f.write("   \n")
            f.write("this is not json\n")
            f.write(json.dumps(_make_record(event_type="TOOL_END")) + "\n")

        result = backfill_log_store.backfill(str(logs_dir), str(db_path))

        assert result.records_imported == 2
        assert result.lines_blank == 2
        assert result.lines_malformed == 1
        assert _row_count(str(db_path)) == 2

    def test_non_dict_json_treated_as_malformed(self, tmp_path):
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        db_path = tmp_path / "store.sqlite"

        path = logs_dir / "agent.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            f.write('"just a string"\n')
            f.write(json.dumps(_make_record()) + "\n")

        result = backfill_log_store.backfill(str(logs_dir), str(db_path))

        assert result.records_imported == 1
        assert result.lines_malformed == 1


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------
class TestBackfillIdempotency:
    def test_second_run_imports_nothing(self, tmp_path):
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        db_path = tmp_path / "store.sqlite"

        records = [
            _make_record(event_type="TOOL_START"),
            _make_record(event_type="TOOL_END"),
        ]
        _write_jsonl(logs_dir / "agent.jsonl", records)

        first = backfill_log_store.backfill(str(logs_dir), str(db_path))
        assert first.records_imported == 2
        assert _row_count(str(db_path)) == 2

        second = backfill_log_store.backfill(str(logs_dir), str(db_path))
        assert second.records_imported == 0
        assert second.records_skipped == 2
        assert _row_count(str(db_path)) == 2

    def test_imported_table_tracks_offsets(self, tmp_path):
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        db_path = tmp_path / "store.sqlite"

        _write_jsonl(logs_dir / "agent.jsonl", [_make_record(), _make_record()])
        backfill_log_store.backfill(str(logs_dir), str(db_path))

        conn = sqlite3.connect(str(db_path))
        try:
            markers = conn.execute("SELECT source, line_no FROM imported ORDER BY line_no").fetchall()
            source = str(logs_dir / "agent.jsonl")
            assert markers == [(source, 1), (source, 2)]
        finally:
            conn.close()

    def test_different_dirs_same_basename_are_distinct(self, tmp_path):
        """Two agent.jsonl files in different dirs must not collide on source key."""
        logs_dir_a = tmp_path / "logs_a"
        logs_dir_b = tmp_path / "logs_b"
        logs_dir_a.mkdir()
        logs_dir_b.mkdir()
        db_path = tmp_path / "store.sqlite"

        _write_jsonl(logs_dir_a / "agent.jsonl", [_make_record(msg="from-a")])
        _write_jsonl(logs_dir_b / "agent.jsonl", [_make_record(msg="from-b")])

        # Import from dir A, then dir B into the same DB.
        first = backfill_log_store.backfill(str(logs_dir_a), str(db_path))
        second = backfill_log_store.backfill(str(logs_dir_b), str(db_path))

        assert first.records_imported == 1
        assert first.records_skipped == 0
        assert second.records_imported == 1
        assert second.records_skipped == 0
        assert _row_count(str(db_path)) == 2


# ---------------------------------------------------------------------------
# Source files untouched
# ---------------------------------------------------------------------------
class TestBackfillSourceIntegrity:
    def test_source_files_not_modified(self, tmp_path):
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        db_path = tmp_path / "store.sqlite"

        plain_path = logs_dir / "agent.jsonl"
        gz_path = logs_dir / "agent.jsonl.2026-07-02.gz"

        _write_jsonl(plain_path, [_make_record()])
        _write_gz_jsonl(gz_path, [_make_record()])

        plain_before = plain_path.read_bytes()
        gz_before = gz_path.read_bytes()

        backfill_log_store.backfill(str(logs_dir), str(db_path))

        assert plain_path.exists()
        assert gz_path.exists()
        assert plain_path.read_bytes() == plain_before
        assert gz_path.read_bytes() == gz_before


# ---------------------------------------------------------------------------
# Text search parity
# ---------------------------------------------------------------------------
class TestBackfillTextSearch:
    def test_imported_row_found_by_extra_needle(self, tmp_path):
        """An imported row is found by instr(search_text, casefold(needle))
        exactly like a live row mapped through record_to_row.
        """
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        db_path = tmp_path / "store.sqlite"

        record = _make_record(tool="custom_tool", msg="Did something")
        _write_jsonl(logs_dir / "agent.jsonl", [record])

        backfill_log_store.backfill(str(logs_dir), str(db_path))

        live_search_text = record_to_row(record)[-1]
        needle = "custom_tool".casefold()

        assert needle in live_search_text

        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.execute(
                "SELECT COUNT(*) FROM events WHERE instr(search_text, ?) > 0",
                (needle,),
            )
            assert cur.fetchone()[0] == 1
        finally:
            conn.close()

    def test_unicode_casefold_in_imported_search_text(self, tmp_path):
        """ß casefolds to ss; the imported search_text must match live mapping."""
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        db_path = tmp_path / "store.sqlite"

        record = _make_record(msg="Straße")
        _write_jsonl(logs_dir / "agent.jsonl", [record])

        backfill_log_store.backfill(str(logs_dir), str(db_path))

        live_search_text = record_to_row(record)[-1]
        needle = "strasse".casefold()

        assert needle in live_search_text

        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.execute(
                "SELECT COUNT(*) FROM events WHERE instr(search_text, ?) > 0",
                (needle,),
            )
            assert cur.fetchone()[0] == 1
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------
class TestBackfillDryRun:
    def test_dry_run_writes_no_rows(self, tmp_path):
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        db_path = tmp_path / "store.sqlite"

        _write_jsonl(logs_dir / "agent.jsonl", [_make_record(), _make_record()])

        result = backfill_log_store.backfill(
            str(logs_dir), str(db_path), dry_run=True
        )

        assert result.records_imported == 2
        assert not db_path.exists()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
class TestBackfillCli:
    def test_main_with_explicit_paths(self, tmp_path, monkeypatch, caplog):
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        db_path = tmp_path / "store.sqlite"

        _write_jsonl(logs_dir / "agent.jsonl", [_make_record()])

        argv = [
            "backfill_log_store.py",
            "--agent-name", "test-agent",
            "--logs-dir", str(logs_dir),
            "--db-path", str(db_path),
        ]

        with caplog.at_level("INFO", logger="backfill_log_store"):
            code = backfill_log_store.main(argv)

        assert code == 0
        assert _row_count(str(db_path)) == 1

    def test_main_dry_run(self, tmp_path, monkeypatch, caplog):
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        db_path = tmp_path / "store.sqlite"

        _write_jsonl(logs_dir / "agent.jsonl", [_make_record()])

        argv = [
            "backfill_log_store.py",
            "--agent-name", "test-agent",
            "--logs-dir", str(logs_dir),
            "--db-path", str(db_path),
            "--dry-run",
        ]

        with caplog.at_level("INFO", logger="backfill_log_store"):
            code = backfill_log_store.main(argv)

        assert code == 0
        assert not db_path.exists()

    def test_main_resolves_xdg_defaults(self, tmp_xdg, tmp_path, monkeypatch, caplog):
        """When --logs-dir/--db-path are omitted, main resolves via xdg_paths."""
        from xdg import xdg_paths

        # tmp_xdg fixture already isolates XDG env; point XDG_STATE_HOME at a
        # dedicated sub-tree for this test so the agent-scoped paths are stable.
        state_home = tmp_xdg / "backfill_state"
        state_home.mkdir(parents=True)
        monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

        agent_name = "cli-test-agent"
        paths = xdg_paths(agent_name)
        paths.state_home.mkdir(parents=True)
        logs_dir = paths.logs_dir
        logs_dir.mkdir(parents=True)
        db_path = paths.log_store

        _write_jsonl(logs_dir / "agent.jsonl", [_make_record()])

        argv = [
            "backfill_log_store.py",
            "--agent-name", agent_name,
        ]

        with caplog.at_level("INFO", logger="backfill_log_store"):
            code = backfill_log_store.main(argv)

        assert code == 0
        assert db_path.exists()
        assert _row_count(str(db_path)) == 1

    def test_main_missing_logs_dir(self, tmp_path, caplog):
        missing_dir = tmp_path / "no-such-logs"
        argv = [
            "backfill_log_store.py",
            "--agent-name", "test-agent",
            "--logs-dir", str(missing_dir),
            "--db-path", str(tmp_path / "store.sqlite"),
        ]

        with caplog.at_level("ERROR", logger="backfill_log_store"):
            code = backfill_log_store.main(argv)

        assert code == 1

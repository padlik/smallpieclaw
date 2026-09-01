"""
backfill_log_store.py
---------------------
One-time CLI tool to import existing JSONL structured-log archives into the
SQLite structured-log store introduced by ``sqlite_log.py``.

The CLI reads ``agent.jsonl`` and every ``agent.jsonl.*.gz`` file in the logs
directory, maps each parsed record to the ``events`` schema via the shared
:func:`sqlite_log.record_to_row` layer, and batch-INSERTs the rows into
``agent_logs.sqlite``.

Additive-only enforcement
~~~~~~~~~~~~~~~~~~~~~~~
Source files are **never** modified, renamed, or deleted. They are opened
read-only (plain text for ``.jsonl``, gzip text for ``.jsonl.*.gz``) and the
CLI performs no write calls against them. Archives remain available as a
forensic fallback after import.

Idempotency
~~~~~~~~~~~
An ``imported`` bookkeeping table records every imported source absolute path
and 1-based line offset. Before a line is imported the offset is checked against
this table; already-imported lines are skipped. The event row and the marker
row are written in the same transaction, so a crash mid-run leaves a consistent
state and re-runs import nothing new.

Malformed/blank lines
~~~~~~~~~~~~~~~~~~~~~
Lines that are blank or fail JSON parsing are reported per-file and in total
but do **not** abort the import. They are **not** recorded in the ``imported``
table, so re-runs will encounter and re-skip them (reported skip counts may
repeat).

Configuration
~~~~~~~~~~~~~
``--config`` defaults to ``config.toml`` and is used only to resolve default
paths via the agent name. Because ``--logs-dir`` and ``--db-path`` can be
provided explicitly, config parsing is intentionally minimal: we only need
``xdg_paths(agent_name)`` to know the default logs directory. No call to
``parse_config`` is required.

Usage examples
--------------
# Import all archives for an agent (uses XDG default logs dir)
python backfill_log_store.py --agent-name piclaw

# Dry-run: count what would be imported without writing
python backfill_log_store.py --agent-name piclaw --dry-run

# Override paths explicitly (no config needed if both are given)
python backfill_log_store.py --agent-name piclaw \\
    --logs-dir /path/to/logs \\
    --db-path /path/to/logs/agent_logs.sqlite

# Verbose per-line progress
python backfill_log_store.py --agent-name piclaw --verbose
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Bootstrap — ensure the repo root is on sys.path regardless of cwd
# ---------------------------------------------------------------------------
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backfill_log_store")

# Shared record-mapping layer from sqlite_log.py.
import sqlite_log  # noqa: E402
from sqlite_log import connect_store, INSERT_EVENT_SQL, record_to_row  # noqa: E402

# ---------------------------------------------------------------------------
# Bookkeeping schema
# ---------------------------------------------------------------------------
_IMPORTED_DDL: str = """
CREATE TABLE IF NOT EXISTS imported (
    source TEXT NOT NULL,
    line_no INTEGER NOT NULL,
    PRIMARY KEY (source, line_no)
);
"""

_INSERT_IMPORTED_SQL: str = """
INSERT OR IGNORE INTO imported (source, line_no) VALUES (?, ?)
"""

# Batch size chosen to balance transaction size and memory use for large archives.
_BATCH_SIZE: int = 100


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------
class _ImportResult:
    """Running counters for a backfill run."""

    def __init__(self) -> None:
        self.files_total: int = 0
        self.files_imported: int = 0
        self.files_skipped: int = 0
        self.records_imported: int = 0
        self.records_skipped: int = 0
        self.lines_malformed: int = 0
        self.lines_blank: int = 0


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------
def _discover_sources(logs_dir: str) -> list[str]:
    """Return the ordered list of JSONL archive sources to import.

    ``agent.jsonl`` is imported first (if present), followed by all
    ``agent.jsonl.*.gz`` files sorted lexicographically. The order matches
    the natural rotation order of the previous JSONL sink.
    """
    base_path = Path(logs_dir) / "agent.jsonl"
    sources: list[str] = []
    if base_path.exists():
        sources.append(str(base_path))

    gz_pattern = os.path.join(logs_dir, "agent.jsonl.*.gz")
    gz_sources = sorted(glob_files(gz_pattern))
    sources.extend(gz_sources)
    return sources


def glob_files(pattern: str) -> list[str]:
    """Glob helper exposed for testability; wraps :func:`glob.glob`."""
    import glob as stdlib_glob  # local import keeps module startup fast
    return stdlib_glob.glob(pattern)


# ---------------------------------------------------------------------------
# Imported-set preloading
# ---------------------------------------------------------------------------
def _load_imported_set(conn: sqlite3.Connection, source: str) -> set[int]:
    """Load the set of already-imported 1-based line offsets for *source*."""
    cur = conn.execute("SELECT line_no FROM imported WHERE source = ?", (source,))
    return {row[0] for row in cur.fetchall()}


# ---------------------------------------------------------------------------
# Per-file import
# ---------------------------------------------------------------------------
def _import_file(
    conn: sqlite3.Connection,
    source_path: str,
    *,
    batch_size: int = _BATCH_SIZE,
    dry_run: bool = False,
    verbose: bool = False,
) -> tuple[int, int, int, int]:
    """Import a single JSONL or gzip JSONL archive.

    Args:
        conn: SQLite connection with the events and imported schemas.
        source_path: Absolute or relative path to the source file.
        batch_size: Number of event rows to INSERT per transaction.
        dry_run: If True, parse and count but do not write to the DB.
        verbose: If True, log every imported line.

    Returns:
        A tuple of ``(imported_count, skipped_count, malformed_count, blank_count)``.
    """
    source_key = os.path.abspath(source_path)
    imported_set = _load_imported_set(conn, source_key) if conn is not None else set()

    imported_count = 0
    skipped_count = 0
    malformed_count = 0
    blank_count = 0

    batch_rows: list[tuple] = []
    batch_markers: list[tuple[str, int]] = []

    def _flush_batch() -> None:
        if dry_run or not batch_rows:
            batch_rows.clear()
            batch_markers.clear()
            return
        with conn:
            conn.executemany(INSERT_EVENT_SQL, batch_rows)
            conn.executemany(_INSERT_IMPORTED_SQL, batch_markers)
        batch_rows.clear()
        batch_markers.clear()

    def _open_source() -> Any:
        if source_path.endswith(".gz"):
            return gzip.open(source_path, "rt", encoding="utf-8", errors="replace")
        return open(source_path, "r", encoding="utf-8", errors="replace")

    with _open_source() as fh:
        for line_no, raw_line in enumerate(fh, start=1):
            line = raw_line.rstrip("\n").rstrip("\r")
            if not line.strip():
                blank_count += 1
                continue

            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                malformed_count += 1
                logger.debug("Malformed line %d in %s", line_no, source_key)
                continue

            if not isinstance(record, dict):
                malformed_count += 1
                logger.debug("Non-dict JSON line %d in %s", line_no, source_key)
                continue

            if line_no in imported_set:
                skipped_count += 1
                continue

            row = record_to_row(record)
            batch_rows.append(row)
            batch_markers.append((source_key, line_no))
            imported_count += 1
            imported_set.add(line_no)

            if verbose:
                logger.info("Importing %s:%d", source_key, line_no)

            if len(batch_rows) >= batch_size:
                _flush_batch()

    _flush_batch()
    return imported_count, skipped_count, malformed_count, blank_count


# ---------------------------------------------------------------------------
# Public backfill runner (also used by tests)
# ---------------------------------------------------------------------------
def backfill(
    logs_dir: str,
    db_path: str,
    *,
    batch_size: int = _BATCH_SIZE,
    dry_run: bool = False,
    verbose: bool = False,
) -> _ImportResult:
    """Import all JSONL archives in *logs_dir* into the store at *db_path*.

    Args:
        logs_dir: Directory containing ``agent.jsonl`` and/or
            ``agent.jsonl.*.gz`` archives.
        db_path: Path to the SQLite store. Created if it does not exist.
        batch_size: Number of rows inserted per transaction.
        dry_run: If True, parse sources and report counts without writing.
        verbose: If True, log per-imported-line details.

    Returns:
        An :class:`_ImportResult` with file and record counters.
    """
    result = _ImportResult()
    sources = _discover_sources(logs_dir)

    if not sources:
        logger.info("No agent.jsonl or agent.jsonl.*.gz sources found in %s — nothing to import.", logs_dir)
        return result

    result.files_total = len(sources)

    if dry_run:
        logger.info("DRY-RUN: parsing %d source file(s) without writing to %s", len(sources), db_path)
    else:
        logger.info("Opening store at %s", db_path)
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)

    conn: sqlite3.Connection | None = None
    if not dry_run:
        conn = connect_store(db_path)
        conn.executescript(sqlite_log.SCHEMA_DDL)
        conn.executescript(_IMPORTED_DDL)

    try:
        for source in sources:
            if not os.path.exists(source):
                result.files_skipped += 1
                logger.warning("Source file not found: %s — skipping.", source)
                continue

            result.files_imported += 1
            logger.info("Importing %s", source)
            try:
                imported, skipped, malformed, blank = _import_file(
                    conn,  # type: ignore[arg-type]
                    source,
                    batch_size=batch_size,
                    dry_run=dry_run,
                    verbose=verbose,
                )
            except OSError as exc:
                result.files_imported -= 1
                result.files_skipped += 1
                logger.error("Could not read %s: %s", source, exc)
                continue

            result.records_imported += imported
            result.records_skipped += skipped
            result.lines_malformed += malformed
            result.lines_blank += blank

            logger.info(
                "  %s: imported=%d skipped=%d malformed=%d blank=%d",
                source,
                imported,
                skipped,
                malformed,
                blank,
            )
    finally:
        if conn is not None:
            conn.close()

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import legacy agent.jsonl archives into the SQLite structured-log store.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--config", default="config.toml",
        help="Path to config.toml (default: config.toml). Used only for default path resolution.",
    )
    parser.add_argument(
        "--agent-name", required=True,
        help="Agent name used to resolve default XDG paths",
    )
    parser.add_argument(
        "--logs-dir", default="",
        help="Override the logs directory (default: XDGPaths.logs_dir for --agent-name)",
    )
    parser.add_argument(
        "--db-path", default="",
        help="Override the SQLite store path (default: <logs_dir>/agent_logs.sqlite)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Parse sources and report counts without writing to the store",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Log every imported line",
    )
    return parser


def _resolve_paths(args: argparse.Namespace) -> tuple[str, str]:
    """Resolve logs_dir and db_path from CLI overrides or XDG defaults."""
    from xdg import xdg_paths

    logs_dir = args.logs_dir
    db_path = args.db_path

    if not logs_dir or not db_path:
        xdg = xdg_paths(args.agent_name)
        if not logs_dir:
            logs_dir = str(xdg.logs_dir)
        if not db_path:
            db_path = str(xdg.log_store)

    return logs_dir, db_path


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    *argv* follows the ``sys.argv`` convention: index 0 is the program name.
    When called programmatically with a synthetic list, include a placeholder
    at index 0 (e.g. ``["backfill_log_store.py", "--agent-name", ...]``) or
    pass the argument strings only and the parser will treat the first element
    as the program name.
    """
    parser = _build_parser()
    if argv is not None and len(argv) > 0 and not argv[0].startswith("-"):
        # First element looks like a program name; strip it so argparse
        # treats the remaining items as real arguments.
        parser.prog = argv[0]
        args = parser.parse_args(argv[1:])
    else:
        args = parser.parse_args(argv)

    logs_dir, db_path = _resolve_paths(args)

    if not os.path.isdir(logs_dir):
        logger.error("Logs directory does not exist: %s", logs_dir)
        return 1

    result = backfill(
        logs_dir=logs_dir,
        db_path=db_path,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )

    _print_summary(result, dry_run=args.dry_run)
    return 0


def _print_summary(result: _ImportResult, *, dry_run: bool) -> None:
    """Print a human-readable summary of the backfill run."""
    print()
    print("=" * 60)
    print("Backfill complete" + (" (DRY-RUN)" if dry_run else ""))
    print("=" * 60)
    print(f"  Source files found:    {result.files_total}")
    print(f"  Source files read:     {result.files_imported}")
    print(f"  Source files skipped:  {result.files_skipped}")
    print(f"  Records imported:      {result.records_imported}")
    print(f"  Records skipped:       {result.records_skipped}")
    print(f"  Malformed lines:       {result.lines_malformed}")
    print(f"  Blank lines:           {result.lines_blank}")
    if dry_run:
        print()
        print("  [DRY-RUN] No rows were written to the store.")
    print("=" * 60)


if __name__ == "__main__":
    sys.exit(main())

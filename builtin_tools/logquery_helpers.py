"""Helpers for the ``log_query`` built-in (structured-log introspection).

Stateless leaf module: depends only on ``logging`` and ``os``; no imports back
into ``builtin_executor`` or any handler module, so it is safe to import eagerly.
"""

from __future__ import annotations

import logging
import os

_WARNING_LEVEL_NUM: int = logging.WARNING
# Option C default view: with no explicit level/event_type filter, surface the
# high-signal lifecycle events below (plus anything WARNING+); routine per-step
# bookkeeping (STEP_*) is omitted unless it is itself WARNING+.
_LOG_QUERY_DEFAULT_INCLUDE_EVENTS: frozenset[str] = frozenset(
    {"TOOL_START", "TOOL_END", "LLM_CALL"}
)
# Bounds for the log_query built-in: cap disk I/O, parse work, and per-field
# size so a mid-loop introspection call cannot blow the context/token budget.
_LOG_QUERY_TAIL_BYTES: int = 1_000_000
_LOG_QUERY_MAX_SCAN_LINES: int = 5000
_LOG_QUERY_FIELD_MAXLEN: int = 500


def _log_level_to_num(level: object) -> int:
    """Map a level NAME or number to its numeric value (unknown/blank -> 0).

    Accepts the lowercase level names emitted by structlog's ``add_log_level``
    (e.g. ``"info"``) as well as standard uppercase names; comparison is
    case-insensitive. Non-numeric/unknown levels sort below every real level.
    """
    if isinstance(level, bool):
        return 0
    if isinstance(level, (int, float)):
        return int(level)
    if not level:
        return 0
    num = logging.getLevelName(str(level).upper())
    return num if isinstance(num, int) else 0


def _log_query_default_keep(rec: dict) -> bool:
    """Option C default-view predicate for a single structured log record.

    Keep the record if it is WARNING+ or a high-signal lifecycle event
    (TOOL_START/TOOL_END/LLM_CALL); routine STEP_* events are dropped unless
    they are themselves WARNING+.
    """
    level_num = _log_level_to_num(rec.get("level"))
    event_type = str(rec.get("event_type", ""))
    is_warn = level_num >= _WARNING_LEVEL_NUM
    return is_warn or event_type in _LOG_QUERY_DEFAULT_INCLUDE_EVENTS


def _read_tail_lines(path: str, max_bytes: int, max_lines: int) -> tuple[list[str], bool]:
    """Return ``(lines, window_saturated)`` for the trailing window of *path*.

    Reads at most the final *max_bytes* and returns at most the most recent
    *max_lines*, so scanning the active log stays cheap regardless of total file
    size. When the read starts mid-file the leading partial line is dropped, so
    callers never see a truncated (unparseable) JSON record.

    ``window_saturated`` is True when the window did NOT cover the whole file —
    either the byte read began mid-file (``max_bytes`` reached) or more lines
    were present than *max_lines* (``max_lines`` reached) — so older records fell
    outside the returned tail and counts derived from it are recent-window lower
    bounds, not full-file totals.
    """
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        start = max(0, size - max_bytes)
        fh.seek(start)
        data = fh.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]  # drop the partial first line from a mid-file seek
    window_saturated = start > 0 or len(lines) > max_lines
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    return lines, window_saturated


def _log_query_project(rec: dict) -> dict:
    """Return a shallow copy of *rec* with over-long string values truncated.

    Caps any single field at ``_LOG_QUERY_FIELD_MAXLEN`` chars so one verbose
    record (e.g. a large ``err`` or ``msg``) cannot dominate the log_query
    output — the field-level analogue of ToolExecutor.max_output.
    """
    projected: dict = {}
    for key, value in rec.items():
        if isinstance(value, str) and len(value) > _LOG_QUERY_FIELD_MAXLEN:
            omitted = len(value) - _LOG_QUERY_FIELD_MAXLEN
            projected[key] = f"{value[:_LOG_QUERY_FIELD_MAXLEN]}…[+{omitted} chars]"
        else:
            projected[key] = value
    return projected

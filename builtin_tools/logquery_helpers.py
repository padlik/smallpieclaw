"""Helpers for the ``log_query`` built-in (structured-log introspection).

Stateless leaf module: depends only on ``json``, ``logging`` and ``os``; no
imports back into ``builtin_executor`` or any handler module, so it is safe to
import eagerly.
"""

from __future__ import annotations

import json
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
    output — the field-level analogue of BuiltinExecutor.max_output.
    """
    projected: dict = {}
    for key, value in rec.items():
        if isinstance(value, str) and len(value) > _LOG_QUERY_FIELD_MAXLEN:
            omitted = len(value) - _LOG_QUERY_FIELD_MAXLEN
            projected[key] = f"{value[:_LOG_QUERY_FIELD_MAXLEN]}…[+{omitted} chars]"
        else:
            projected[key] = value
    return projected


class LogQueryFilters:
    """Immutable collection of resolved ``log_query`` filter parameters.

    Carries the normalized values used to build a record predicate.  All
    string filters are stored as plain strings; callers are responsible for
    resolving trace scope and the ``text`` alias before constructing the
    object.
    """

    def __init__(  # noqa: PLR0913
        self,
        *,
        trace: str = "",
        all_traces: bool = False,
        level: str = "",
        min_level: int = 0,
        event_type: str = "",
        tool: str = "",
        since: str = "",
        prompt_id: object = None,
        text: str = "",
        use_default_view: bool = False,
    ) -> None:
        self.trace = trace
        self.all_traces = all_traces
        self.level = level
        self.min_level = min_level
        self.event_type = event_type
        self.tool = tool
        self.since = since
        self.prompt_id = prompt_id
        self.text = text
        self.use_default_view = use_default_view

    @property
    def text_folded(self) -> str:
        """Unicode case-folded text for case-insensitive full-record search."""
        return self.text.casefold() if self.text else ""


def _build_log_query_predicate(filters: LogQueryFilters) -> callable:
    """Build a single-record predicate from resolved ``log_query`` filters.

    The returned callable accepts a structured log record (``dict``) and
    returns ``True`` only when the record satisfies every active filter.

    Behavior mirrors the legacy inline filtering in ``_exec_log_query``:

    * ``trace`` exact match unless ``all_traces`` is set; missing/empty record
      trace fields compare as empty strings.
    * ``prompt_id`` compares both sides coerced to strings; a ``None`` filter
      value disables the check.
    * ``since`` is a string-prefix comparison against the record's ``ts`` field.
    * ``tool``/``event_type`` are exact matches when supplied.
    * ``level`` requires the record's numeric level to be ``>= min_level``.
    * ``use_default_view`` applies the Option C high-signal predicate unless a
      text search is active (callers set ``use_default_view`` accordingly).
    * ``text`` performs a Unicode case-insensitive substring search over the
      compact JSON serialization of the record.
    """
    text_folded = filters.text_folded

    def predicate(rec: dict) -> bool:
        if not filters.all_traces and str(rec.get("trace", "")) != filters.trace:
            return False
        if filters.prompt_id is not None:
            rec_prompt_id = rec.get("prompt_id")
            if str(rec_prompt_id) != str(filters.prompt_id):
                return False
        if filters.since and str(rec.get("ts", "")) < filters.since:
            return False
        if filters.tool and rec.get("tool") != filters.tool:
            return False
        if filters.level and _log_level_to_num(rec.get("level")) < filters.min_level:
            return False
        if filters.event_type and rec.get("event_type") != filters.event_type:
            return False
        if filters.use_default_view and not _log_query_default_keep(rec):
            return False
        if text_folded and text_folded not in json.dumps(rec, ensure_ascii=False).casefold():
            return False
        return True

    return predicate


def _parse_log_query_lines(lines: list[str]) -> list[dict]:
    """Parse raw JSONL lines into record dicts, skipping malformed/blank lines."""
    records: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records


def filter_log_lines(lines: list[str], filters: LogQueryFilters) -> list[dict]:
    """Parse *lines* and return all records matching *filters*.

    This is the reusable filtering core extracted from ``_exec_log_query``.
    It separates JSONL parsing (with graceful skipping of malformed or blank
    lines) from predicate evaluation, so callers only need to open the log and
    supply a ``LogQueryFilters`` object.
    """
    predicate = _build_log_query_predicate(filters)
    return [rec for rec in _parse_log_query_lines(lines) if predicate(rec)]

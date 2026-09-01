"""Helpers for the ``log_query`` built-in (structured-log introspection).

Stateless leaf module: depends only on ``json``, ``logging`` and ``sqlite_log``
constants for SQL compilation; no imports back into ``builtin_executor`` or any
handler module, so it is safe to import eagerly.
"""

from __future__ import annotations

import logging

_WARNING_LEVEL_NUM: int = logging.WARNING
# Per-field size cap so one verbose record cannot dominate the log_query output.
_LOG_QUERY_FIELD_MAXLEN: int = 500

# Hard upper bound on the number of records a single log_query call may
# materialize. Keeps an LLM-reachable query from pulling the entire 30-day
# store into memory before the max_output size cap runs.
_LOG_QUERY_MAX_LIMIT: int = 500

# Closed, ordered level names matching the numeric values returned by
# :func:`_log_level_to_num`. Used to compile a min-level filter into the
# equivalent ``level IN (...)`` SQL clause over the lowercase stored names.
_LOG_QUERY_LEVEL_ORDER: tuple[tuple[str, int], ...] = (
    ("debug", 10),
    ("info", 20),
    ("warning", 30),
    ("error", 40),
    ("critical", 50),
)

# Authoritative six-event set for the Option C default view (D5).
_LOG_QUERY_DEFAULT_EVENTS: tuple[str, ...] = (
    "TOOL_START",
    "TOOL_END",
    "TOOL_FAILED",
    "LLM_CALL",
    "LLM_FAILED",
    "ERROR",
)


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


def _log_query_level_names(min_level: int) -> list[str]:
    """Return the closed ordered set of stored level names >= *min_level*.

    The result is used to build ``level IN (...)`` SQL clauses. Unknown or
    blank level arguments map to ``min_level == 0`` and therefore include all
    known levels, mirroring the legacy ``>=`` numeric comparison for records
    whose level name is recognised.
    """
    return [name for name, num in _LOG_QUERY_LEVEL_ORDER if num >= min_level]


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

    Carries the normalized values used to build a SQL WHERE clause. All
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


def _log_query_compile_where(filters: LogQueryFilters) -> tuple[str, list]:
    """Compile active filters into a SQLite WHERE clause and parameter list.

    The returned *where* string contains no ``ORDER BY`` or ``LIMIT`` clauses;
    callers append those as needed. The parameters are ordered to match the
    placeholders left in the clause.

    Filter semantics (per design D5):

    * ``trace``: sargable exact match via ``trace = ? OR (? = '' AND trace IS NULL)``;
      omitted when ``all_traces`` is true. The first branch uses ``idx_trace``;
      the second branch preserves legacy parity where records with a missing
      trace field match an empty-string filter value.
    * ``level`` (min): ``level IN (...)`` over the ordered set of stored
      lowercase names whose numeric value is >= ``min_level``.
    * ``event_type``: ``event_type = ?``.
    * ``tool``: ``json_extract(extra, '$.tool') = ?``.
    * ``since``: ``ts >= ?`` (ISO prefix comparison).
    * ``text``: ``instr(search_text, ?) > 0`` with the casefolded needle;
      ``%``/``_`` are ordinary characters.
    * ``prompt_id``: exact match via ``prompt_id = ?``; omitted when the
      filter value is ``None``. SQLite type coercion handles numeric/string
      equivalence the same way the legacy ``str(a) == str(b)`` check did.
    * ``use_default_view``: adds the Option C clause
      ``event_type IN (six events) OR level IN ('warning','error','critical')``.
    """
    parts: list[str] = []
    params: list = []

    if not filters.all_traces:
        # Sargable trace filter: the indexed `trace = ?` branch is used first;
        # the `trace IS NULL` branch only fires for the legacy empty-string case.
        parts.append("(trace = ? OR (? = '' AND trace IS NULL))")
        params.append(filters.trace)
        params.append(filters.trace)

    if filters.level:
        levels = _log_query_level_names(filters.min_level)
        placeholders = ", ".join("?" for _ in levels)
        parts.append(f"level IN ({placeholders})")
        params.extend(levels)

    if filters.event_type:
        parts.append("event_type = ?")
        params.append(filters.event_type)

    if filters.tool:
        parts.append("json_extract(extra, '$.tool') = ?")
        params.append(filters.tool)

    if filters.since:
        parts.append("ts >= ?")
        params.append(filters.since)

    if filters.prompt_id is not None:
        parts.append("prompt_id = ?")
        params.append(filters.prompt_id)

    text_folded = filters.text_folded
    if text_folded:
        parts.append("instr(search_text, ?) > 0")
        params.append(text_folded)

    if filters.use_default_view:
        event_placeholders = ", ".join("?" for _ in _LOG_QUERY_DEFAULT_EVENTS)
        level_names = _log_query_level_names(_WARNING_LEVEL_NUM)
        level_placeholders = ", ".join("?" for _ in level_names)
        parts.append(
            f"(event_type IN ({event_placeholders}) OR level IN ({level_placeholders}))"
        )
        params.extend(_LOG_QUERY_DEFAULT_EVENTS)
        params.extend(level_names)

    where = " AND ".join(parts) if parts else "1"
    return where, params

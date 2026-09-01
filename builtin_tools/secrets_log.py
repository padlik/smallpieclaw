"""Secret-vault and structured-log introspection built-ins.

Handler module holding two tool groups:

* ``SecretsTools`` — the ``secret_get`` vault lookup (interactive confirmation at
  depth 0, headless operator bridge for sub-agents). It reads ``_vault_path`` and
  stages confirmation through the ``owner`` façade at call time; the
  ``config_schema``/``exceptions`` imports stay function-local (ADR-0003 vault).
* ``LogQueryTools`` — the read-only ``log_query`` introspection over the SQLite
  structured log store, reading ``_log_store_path`` and ``max_output`` via ``owner``.

The ``builtin_executor`` import is under ``TYPE_CHECKING`` only (no runtime cycle).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import TYPE_CHECKING

import structlog

from builtin_tools.logquery_helpers import (
    LogQueryFilters,
    _log_level_to_num,
    _log_query_compile_where,
    _log_query_project,
    _LOG_QUERY_MAX_LIMIT,
)
from sqlite_log import connect_store, row_to_record

if TYPE_CHECKING:
    from builtin_executor import BuiltinExecutor

logger = logging.getLogger(__name__)


class SecretsTools:
    """Vault ``secret_get`` handler; delegates confirmation to the owner façade."""

    def __init__(self, owner: BuiltinExecutor) -> None:
        self._owner = owner

    def _exec_secret_get(self, args: dict, caller_depth: int = 0, caller_tag: str = "") -> dict:
        """Stage a vault lookup for operator confirmation."""
        key = args.get("key", "")
        if not key:
            return {
                "success": False,
                "output": "",
                "error": "secret_get: 'key' is required.",
                "exit_code": -1,
            }
        desc = f"Look up vault key '{key}'"
        if caller_depth == 0:
            return self._owner._requires_confirmation(
                "secret_get", args, desc, caller_depth=caller_depth, caller_tag=caller_tag
            )
        return self._owner._headless_confirm_bridge(
            "secret_get", args, desc, caller_tag=caller_tag
        )

    def _run_secret_get(self, args: dict, caller_tag: str = "") -> dict:
        """Read a confirmed key from the TOML vault file.

        Delegates format parsing to :func:`config_schema.parse_vault_content`
        with ``require_all_strings=False`` so a non-string SIBLING key (e.g. an
        idiomatic ``[jira]`` table) no longer breaks unrelated lookups — only
        the requested key must be a string.  The returned value is always a
        string: if the requested key itself resolves to a non-string type, a
        ``config_error`` result is returned instead of the raw value.
        """
        # Local imports to avoid circular-import risk at module load time.
        from config_schema import parse_vault_content as _parse_vault_content  # noqa: PLC0415
        from exceptions import ConfigError as _ConfigError  # noqa: PLC0415

        key = args.get("key", "")
        logger.info("Built-in secret_get: key=%s", key)

        try:
            with open(self._owner._vault_path, encoding="utf-8") as fh:
                content = fh.read()
        except OSError as exc:
            return {
                "success": False,
                "output": "",
                "error": f"Cannot read vault: {exc}",
                "exit_code": -1,
                "error_type": "config_error",
                "recoverable": False,
                "suggestion": "Check vault file path and TOML validity.",
            }

        try:
            vault = _parse_vault_content(
                content, self._owner._vault_path, require_all_strings=False
            )
        except _ConfigError as exc:
            # Normalise ConfigError messages to begin with "Cannot read vault:"
            # so the tool API surface stays stable.
            msg = str(exc)
            if not msg.startswith("Cannot read vault"):
                msg = f"Cannot read vault: {msg}"
            return {
                "success": False,
                "output": "",
                "error": msg,
                "exit_code": -1,
                "error_type": "config_error",
                "recoverable": False,
                "suggestion": "Check vault file path and TOML validity.",
            }

        value = vault.get(key)
        if value is None and key not in vault:
            return {
                "success": False,
                "output": "",
                "error": f"Vault key '{key}' not found.",
                "exit_code": -1,
                "error_type": "not_found",
                "recoverable": False,
                "suggestion": "Add the key to the vault file.",
            }

        # Per-key type check: siblings may be non-string (require_all_strings=False),
        # but the value we hand back must be a string secret.
        if not isinstance(value, str):
            return {
                "success": False,
                "output": "",
                "error": (
                    f"Vault key '{key}' is not a string secret "
                    f"(got {type(value).__name__})."
                ),
                "exit_code": -1,
                "error_type": "config_error",
                "recoverable": False,
                "suggestion": (
                    "Store the secret as a top-level string key "
                    '(e.g. api_key = "sk-...") in the vault file.'
                ),
            }

        # value is guaranteed to be a string by the per-key check above.
        return {
            "success": True,
            "output": value,
            "error": "",
            "exit_code": 0,
            "error_type": "",
            "recoverable": True,
        }


class LogQueryTools:
    """Read-only ``log_query`` introspection over the SQLite log store."""

    def __init__(self, owner: BuiltinExecutor) -> None:
        self._owner = owner

    def _exec_log_query(self, args: dict, caller_depth: int = 0, caller_tag: str = "") -> dict:
        """Query the SQLite structured log store and return matching records.

        Read-only introspection over ``owner._log_store_path``. Queries the
        full 30-day retention window using indexed SQL filters. Supports
        trace/level/event_type/tool/since/text/prompt_id filters, a useful
        default view (Option C) when neither level, event_type, nor text is
        supplied, and most-recent-N truncation via ``limit``.

        The ``text`` argument (alias: ``query``) performs a Unicode-aware
        case-insensitive (casefold) substring search against the compact JSON
        serialisation stored in ``search_text`` so that any key or value — msg,
        event, logger, tool output, etc. — is searchable.

        When ``text`` is provided without an explicit ``level`` or
        ``event_type``, the Option C high-signal default view is **not** applied,
        allowing routine INFO startup records (e.g. "GraphMemoryStore
        initialised at data/graph_memory (dim=1536)") to be surfaced.

        When ``text``/``query`` or ``prompt_id`` is given and the caller did
        **not** supply an explicit ``trace`` argument, the scope is
        automatically widened to all traces (equivalent to ``trace='*'``).
        This ensures that startup records — which often carry no trace tag or
        a different trace — or records for a specific prompt ID are found
        without the caller needing to know the current run's trace. Passing an
        explicit ``trace`` always overrides this auto-widening.

        A missing/empty store path or an unreadable store file yields a
        well-formed EMPTY result rather than an error. ``caller_depth`` and
        ``caller_tag`` are accepted for dispatch symmetry with peer handlers.
        """
        # limit (most-recent-N kept); fall back to the default on bad input.
        try:
            limit = int(args.get("limit", 50))
        except (TypeError, ValueError):
            limit = 50
        if limit <= 0:
            limit = 50
        limit = min(limit, _LOG_QUERY_MAX_LIMIT)

        level_arg = args.get("level") or ""
        event_type_arg = args.get("event_type") or ""
        tool_arg = args.get("tool") or ""
        since_arg = str(args.get("since") or "")
        # prompt_id: exact match against the first-class prompt_id column.
        prompt_id_arg = args.get("prompt_id")
        # text/query: Unicode-aware case-insensitive full-record substring search.
        # Accept "query" as an alias for "text"; "text" takes precedence.
        text_arg = str(args.get("text") or args.get("query") or "").strip()
        # Option C default view is suppressed when text/query is given so that
        # INFO-level records (e.g. startup messages) are not silently excluded.
        use_default_view = not level_arg and not event_type_arg and not text_arg
        min_level = _log_level_to_num(level_arg) if level_arg else 0
        # casefold gives correct Unicode case-folding (e.g. German ß → ss).
        # Trace scope resolution (priority: explicit arg > auto-widen > current-run).
        #
        # When text/query or prompt_id is given and the caller did NOT supply a
        # non-null ``trace`` value, the scope auto-widens to all traces so that
        # startup records (no trace or a different trace) or a specific prompt's
        # records are surfaced. An explicit non-null trace always overrides
        # this; JSON null is treated as unset because LLM function calls may
        # emit it for omitted optional parameters.
        trace_val = args.get("trace")
        if trace_val is not None:
            trace = str(trace_val)
            all_traces = trace in ("*", "")
        elif text_arg or prompt_id_arg is not None:
            trace = "*"
            all_traces = True
        else:
            trace = str(structlog.contextvars.get_contextvars().get("trace", "") or "")
            all_traces = trace in ("*", "")

        logger.info(
            "log_query: trace=%s level=%s event_type=%s tool=%s since=%s text=%s "
            "prompt_id=%s limit=%d",
            trace or "<all>", level_arg or "-", event_type_arg or "-",
            tool_arg or "-", since_arg or "-", text_arg or "-",
            prompt_id_arg or "-", limit,
        )

        path = self._owner._log_store_path
        if not path:
            return self._log_query_result([], 0, False)

        try:
            conn = connect_store(path)
            conn.execute("PRAGMA busy_timeout = 3000")
        except (OSError, sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
            logger.warning("log_query: cannot open log store %s: %s", path, exc)
            return self._log_query_result([], 0, False)

        filters = LogQueryFilters(
            trace=trace,
            all_traces=all_traces,
            level=level_arg,
            min_level=min_level,
            event_type=event_type_arg,
            tool=tool_arg,
            since=since_arg,
            prompt_id=prompt_id_arg,
            text=text_arg,
            use_default_view=use_default_view,
        )
        where, params = _log_query_compile_where(filters)

        try:
            total_matched = conn.execute(
                f"SELECT COUNT(*) FROM events WHERE {where}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"""
                SELECT * FROM (
                    SELECT * FROM events WHERE {where}
                    ORDER BY ts DESC, id DESC LIMIT ?
                ) ORDER BY ts ASC, id ASC
                """,
                params + [limit],
            ).fetchall()
        except (OSError, sqlite3.Error) as exc:
            logger.warning("log_query: query failed for store %s: %s", path, exc)
            return self._log_query_result([], 0, False)
        finally:
            try:
                conn.close()
            except sqlite3.Error:
                pass

        records = [row_to_record(row) for row in rows]
        truncated = total_matched > limit
        return self._log_query_result(records, total_matched, truncated)

    def _log_query_result(self, records: list, total_matched: int, truncated: bool) -> dict:
        """Render a log_query payload using the peer result-dict convention.

        Records are projected (over-long field values truncated) and only the
        most recent records whose compact serialization fits within
        ``owner.max_output`` are kept — mirroring BuiltinExecutor.max_output so a
        mid-loop call cannot blow the context budget. The metadata keys (count,
        truncated, total_matched) are preserved; ``truncated`` also reflects any
        size cap. The newest record is always kept even if it alone is large.

        ``total_matched`` is exact across the full retention window (30 days)
        thanks to the ``COUNT(*)`` query; there are no window-saturation or
        scanned-line counters.
        """
        projected = [_log_query_project(rec) for rec in records]
        # Single pass newest→oldest: keep records until the serialized size would
        # exceed the budget (O(n); avoids re-serializing the whole list).
        kept_rev: list = []
        size = 2  # the enclosing "[]"
        for rec in reversed(projected):
            size += len(json.dumps(rec, ensure_ascii=False)) + 1  # +1 separator
            if kept_rev and size > self._owner.max_output:
                truncated = True
                break
            kept_rev.append(rec)
        kept = list(reversed(kept_rev))
        payload = {
            "records": kept,
            "count": len(kept),
            "truncated": truncated,
            "total_matched": total_matched,
        }
        return {
            "success": True,
            "output": json.dumps(payload, ensure_ascii=False),
            "error": "",
            "exit_code": 0,
            "error_type": "",
            "recoverable": True,
        }



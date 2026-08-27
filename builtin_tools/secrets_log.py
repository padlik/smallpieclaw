"""Secret-vault and structured-log introspection built-ins.

Handler module holding two tool groups:

* ``SecretsTools`` — the ``secret_get`` vault lookup (interactive confirmation at
  depth 0, headless operator bridge for sub-agents). It reads ``_vault_path`` and
  stages confirmation through the ``owner`` façade at call time; the
  ``config_schema``/``exceptions`` imports stay function-local (ADR-0003 vault).
* ``LogQueryTools`` — the read-only ``log_query`` introspection over the active
  JSONL sink, reading ``_log_jsonl_path`` and ``max_output`` via ``owner``.

The ``builtin_executor`` import is under ``TYPE_CHECKING`` only (no runtime cycle).
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING

import structlog

from builtin_tools.logquery_helpers import (
    _LOG_QUERY_MAX_SCAN_LINES,
    _LOG_QUERY_TAIL_BYTES,
    LogQueryFilters,
    _log_level_to_num,
    _log_query_project,
    _read_tail_lines,
    filter_log_lines,
)

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
    """Read-only ``log_query`` introspection over the active JSONL log sink."""

    def __init__(self, owner: BuiltinExecutor) -> None:
        self._owner = owner

    def _exec_log_query(self, args: dict, caller_depth: int = 0, caller_tag: str = "") -> dict:
        """Query the active JSONL log sink and return matching records.

        Read-only introspection over ``owner._log_jsonl_path`` (one JSON object
        per line). Only the trailing ``_LOG_QUERY_TAIL_BYTES`` bytes / most
        recent ``_LOG_QUERY_MAX_SCAN_LINES`` lines are scanned, so a mid-loop
        call does bounded work regardless of total log size (``total_matched``
        therefore counts matches within that tail window). Supports
        trace/level/event_type/tool/since/text filters, a useful default view
        (Option C) when neither level, event_type, nor text is supplied, and
        most-recent-N truncation via ``limit``.

        The ``text`` argument (alias: ``query``) performs a Unicode-aware
        case-insensitive (casefold) substring search against the compact JSON
        serialisation of each record so that any key or value — msg, event,
        logger, tool output, etc. — is searchable.

        When ``text`` is provided without an explicit ``level`` or
        ``event_type``, the Option C high-signal default view is **not** applied,
        allowing routine INFO startup records (e.g. "GraphMemoryStore
        initialised at data/graph_memory (dim=1536)") to be surfaced.

        When ``text``/``query`` is given and the caller did **not** supply an
        explicit ``trace`` argument, the scope is automatically widened to all
        traces (equivalent to ``trace='*'``). This ensures that startup records
        — which often carry no trace tag or a different trace — are found by a
        bare ``{"text": "…"}`` call without the caller needing to know the
        right trace. Passing an explicit ``trace`` always overrides this
        auto-widening.

        A missing or unset log path yields a well-formed EMPTY result rather
        than an error. ``caller_depth`` and ``caller_tag`` are accepted for
        dispatch symmetry with peer handlers.
        """
        # limit (most-recent-N kept); fall back to the default on bad input.
        try:
            limit = int(args.get("limit", 50))
        except (TypeError, ValueError):
            limit = 50
        if limit <= 0:
            limit = 50

        level_arg = args.get("level") or ""
        event_type_arg = args.get("event_type") or ""
        tool_arg = args.get("tool") or ""
        since_arg = str(args.get("since") or "")
        # prompt_id: exact match against the first-class prompt_id field.
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
        # records are surfaced.  An explicit non-null trace always overrides
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

        path = self._owner._log_jsonl_path
        if not path or not os.path.exists(path):
            return self._log_query_result([], 0, False)

        # Bounded tail read: never scan more than the trailing window even if the
        # active log has grown large within the day (before rotation).
        try:
            lines, window_saturated = _read_tail_lines(
                path, _LOG_QUERY_TAIL_BYTES, _LOG_QUERY_MAX_SCAN_LINES
            )
        except OSError as exc:
            logger.warning("log_query: cannot read log sink %s: %s", path, exc)
            return self._log_query_result([], 0, False)
        scanned_lines = len(lines)

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
        matched = filter_log_lines(lines, filters)

        total_matched = len(matched)
        truncated = total_matched > limit
        out_records = matched[-limit:] if truncated else matched
        return self._log_query_result(
            out_records, total_matched, truncated,
            window_saturated=window_saturated, scanned_lines=scanned_lines,
        )

    def _log_query_result(self, records: list, total_matched: int, truncated: bool,
                          *, window_saturated: bool = False, scanned_lines: int = 0) -> dict:
        """Render a log_query payload using the peer result-dict convention.

        Records are projected (over-long field values truncated) and only the
        most recent records whose compact serialization fits within
        ``owner.max_output`` are kept — mirroring BuiltinExecutor.max_output so a
        mid-loop call cannot blow the context budget. The metadata keys (count,
        truncated, total_matched) are preserved; ``truncated`` also reflects any
        size cap. The newest record is always kept even if it alone is large.

        ``window_saturated``/``scanned_lines`` disclose the recent-window scope:
        ``total_matched`` counts matches only within the ``scanned_lines`` lines
        of the scanned tail, and when ``window_saturated`` is True older records
        fell outside that window (so it is a recent-window lower bound).
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
            "window_saturated": window_saturated,
            "scanned_lines": scanned_lines,
        }
        return {
            "success": True,
            "output": json.dumps(payload, ensure_ascii=False),
            "error": "",
            "exit_code": 0,
            "error_type": "",
            "recoverable": True,
        }

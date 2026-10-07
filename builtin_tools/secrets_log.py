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
import os
import re
import sqlite3
import tempfile
from typing import TYPE_CHECKING, Iterable

import structlog

from builtin_tools.logquery_helpers import (
    LogQueryFilters,
    _log_level_to_num,
    _log_query_compile_where,
    _log_query_project,
    _LOG_QUERY_MAX_LIMIT,
)
from builtin_tools.text_utils import (
    build_detection_probes,
    build_redaction_needles,
    redact_structure,
)
from sqlite_log import _compact_json, connect_store, row_to_record

if TYPE_CHECKING:
    from builtin_executor import BuiltinExecutor

logger = logging.getLogger(__name__)

# Hard cap on the serialized size of a single log_detail record. Records above
# this limit dump to the results directory and return only a preview + pointer.
_LOG_DETAIL_MAX_CHARS: int = 65536

# SQLite INTEGER is a signed 64-bit value; ids/before_id above this bound
# cannot exist in the store and would raise OverflowError at bind time.
_SQLITE_INT64_MAX: int = 2**63 - 1

# Valid trace-id format used to sanitise a stored record's trace before using it
# as a results-directory path component. Stored content is untrusted; malformed
# values fall back to the current run's trace context.
_TRACE_ID_RE = re.compile(r"r-[0-9a-f]{8}")


def _scan_dirty_rows(
    rows: Iterable[sqlite3.Row],
    secrets_list: list[str],
    text_folded: str,
    before_id: int | None,
    limit: int,
) -> tuple[int, list[tuple[int, dict]]]:
    """Match *rows* (newest id first) against the redacted corpus in one streaming pass.

    Returns the exact number of rows whose vault-redacted, casefolded compact
    JSON contains *text_folded* and, newest first, only the first *limit* of
    those below *before_id*. Because the traversal is descending, those are the
    newest eligible matches, so memory stays bounded by *limit* however many
    rows match.
    """
    count = 0
    retained: list[tuple[int, dict]] = []
    for row in rows:
        redacted = redact_structure(
            row_to_record(row), secrets_list, preserve_top_level_keys=True
        )
        corpus = _compact_json(
            {k: v for k, v in redacted.items() if k != "id"}
        ).casefold()
        if text_folded not in corpus:
            continue
        count += 1
        row_id = row["id"]
        if len(retained) < limit and (before_id is None or row_id < before_id):
            retained.append((row_id, redacted))
    return count, retained


class SecretsTools:
    """Vault ``secret_get`` handler; delegates confirmation to the owner façade."""

    def __init__(self, owner: BuiltinExecutor) -> None:
        self._owner = owner

    def _exec_secret_get(self, args: dict, caller_depth: int = 0, caller_tag: str = "",
                         scheduled: bool = False) -> dict:
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
        if scheduled:
            # Scheduled runs never reach the Telegram bridge: the seam denies
            # noninteractively before any token is staged.
            return self._owner._requires_confirmation(
                "secret_get", args, desc, caller_depth=caller_depth, caller_tag=caller_tag,
                scheduled=True,
            )
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
        supplied, most-recent-N truncation via ``limit``, and ``before_id``
        paging for gapless newest→oldest enumeration.

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

        ``before_id`` restricts the page to records with ``id < before_id``
        while leaving ``total_matched`` as the count of the entire match set.
        Pages are always ordered by ``id`` (newest id first, re-ascended for
        output), so consecutive ``before_id`` pages are gapless even when
        emit-time ``ts`` order and drain-order ``id`` diverge.

        A missing/empty store path or an unreadable store file yields a
        well-formed EMPTY result rather than an error. ``caller_depth`` and
        ``caller_tag`` are accepted for dispatch symmetry with peer handlers.
        """
        # limit (most-recent-N kept); fall back to the default on bad input.
        # OverflowError covers non-finite floats (JSON 1e999 → inf), whose
        # int() coercion raises OverflowError, not ValueError.
        try:
            limit = int(args.get("limit", 50))
        except (TypeError, ValueError, OverflowError):
            limit = 50
        if limit <= 0:
            limit = 50
        limit = min(limit, _LOG_QUERY_MAX_LIMIT)

        # before_id: primary-key page anchor. JSON null/absent/non-int/<=0 —
        # and non-finite floats, whose int() coercion raises OverflowError —
        # omit the filter, mirroring the graceful limit fallback. Values above
        # the SQLite signed-64-bit bound also omit the filter (they cannot
        # match any real row id and would raise OverflowError at bind time).
        before_id_raw = args.get("before_id")
        before_id: int | None = None
        if before_id_raw is not None and not isinstance(before_id_raw, bool):
            try:
                before_id = int(before_id_raw)
            except (TypeError, ValueError, OverflowError):
                before_id = None
        if before_id is not None and before_id <= 0:
            before_id = None
        if before_id is not None and before_id > _SQLITE_INT64_MAX:
            before_id = None

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
            "prompt_id=%s limit=%d before_id=%s",
            trace or "<all>", level_arg or "-", event_type_arg or "-",
            tool_arg or "-", since_arg or "-", text_arg or "-",
            prompt_id_arg or "-", limit,
            before_id if before_id is not None else "-",
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
            before_id=before_id,
        )
        where, params = _log_query_compile_where(filters)
        # COUNT(*) reports the whole match set, excluding the before_id anchor.
        count_filters = LogQueryFilters(
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
        where_count, params_count = _log_query_compile_where(count_filters)

        # Read-time pass against the CURRENT vault before projection/truncation
        # (same two-layer trust model as log_detail): field truncation alone
        # does not protect a credential shorter than the field cap.
        secrets_list = self._current_vault_secrets()
        needles = build_redaction_needles(secrets_list)

        try:
            if filters.text_folded and needles:
                total_matched, records = self._log_query_redacted_text_search(
                    conn, filters, count_filters, secrets_list, limit, before_id
                )
            else:
                total_matched = conn.execute(
                    f"SELECT COUNT(*) FROM events WHERE {where_count}", params_count
                ).fetchone()[0]
                rows = conn.execute(
                    f"""
                    SELECT * FROM (
                        SELECT * FROM events WHERE {where}
                        ORDER BY id DESC LIMIT ?
                    ) ORDER BY id ASC
                    """,
                    params + [limit],
                ).fetchall()
                records = [
                    redact_structure(
                        row_to_record(row), secrets_list, preserve_top_level_keys=True
                    )
                    for row in rows
                ]
        except (OSError, sqlite3.Error) as exc:
            logger.warning("log_query: query failed for store %s: %s", path, exc)
            return self._log_query_result([], 0, False)
        finally:
            try:
                conn.close()
            except sqlite3.Error:
                pass

        if before_id is not None:
            for rec in records:
                rec_id = rec.get("id")
                if not isinstance(rec_id, int) or rec_id >= before_id:
                    logger.warning(
                        "log_query: returned record id=%s violates before_id=%s",
                        rec_id, before_id,
                    )
        truncated = total_matched > limit
        return self._log_query_result(records, total_matched, truncated)

    def _log_query_redacted_text_search(
        self,
        conn: sqlite3.Connection,
        filters: LogQueryFilters,
        count_filters: LogQueryFilters,
        secrets_list: list[str],
        limit: int,
        before_id: int | None,
    ) -> tuple[int, list[dict]]:
        """Run a ``text=`` search against the vault-redacted corpus.

        The stored ``search_text`` predates the current vault, so matching it in
        SQL would let counts reveal credential prefixes. Rows are split by
        whether their ``search_text`` contains any detection probe (the
        casefolded current-vault needles at every JSON-escape depth redaction
        covers plus one for the serialization layer, and the deep-nesting
        marker; see :func:`build_detection_probes`):

        * clean rows cannot be altered by redaction, so their stored corpus IS
          the redacted corpus and the indexed SQL ``instr`` match is exact;
        * dirty rows are matched in Python against the casefolded compact JSON
          of the redacted record (``id`` excluded, as at ingestion).

        ``total_matched`` is the exact union count. The page is the newest
        ``limit`` of the union, honouring ``before_id``, so paging is gapless.
        """
        probes = build_detection_probes(secrets_list)
        dirty_sql = "(" + " OR ".join("instr(search_text, ?) > 0" for _ in probes) + ")"

        base_where, base_params = _log_query_compile_where(
            LogQueryFilters(
                trace=count_filters.trace, all_traces=count_filters.all_traces,
                level=count_filters.level, min_level=count_filters.min_level,
                event_type=count_filters.event_type, tool=count_filters.tool,
                since=count_filters.since, prompt_id=count_filters.prompt_id,
                use_default_view=count_filters.use_default_view,
            )
        )
        text_folded = filters.text_folded

        clean_where = f"{base_where} AND NOT {dirty_sql} AND instr(search_text, ?) > 0"
        clean_params = [*base_params, *probes, text_folded]
        clean_count = conn.execute(
            f"SELECT COUNT(*) FROM events WHERE {clean_where}", clean_params
        ).fetchone()[0]

        anchor = ""
        anchor_params: list = []
        if before_id is not None and before_id > 0:
            anchor, anchor_params = " AND id < ?", [before_id]
        clean_rows = conn.execute(
            f"SELECT * FROM events WHERE {clean_where}{anchor} ORDER BY id DESC LIMIT ?",
            [*clean_params, *anchor_params, limit],
        ).fetchall()

        dirty_count, dirty_retained = _scan_dirty_rows(
            conn.execute(
                f"SELECT * FROM events WHERE {base_where} AND {dirty_sql} ORDER BY id DESC",
                [*base_params, *probes],
            ),
            secrets_list,
            text_folded,
            before_id,
            limit,
        )

        page = [
            (row["id"], redact_structure(
                row_to_record(row), secrets_list, preserve_top_level_keys=True
            ))
            for row in clean_rows
        ]
        page += dirty_retained
        page.sort(key=lambda item: item[0], reverse=True)
        newest = page[:limit]
        newest.reverse()
        return clean_count + dirty_count, [rec for _rid, rec in newest]

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

    def _log_detail_empty(self) -> dict:
        """Return the well-formed EMPTY log_detail payload in the peer convention."""
        return {
            "success": True,
            "output": json.dumps(
                {"found": False, "record": None, "dumped": False, "dump_path": None},
                ensure_ascii=False,
            ),
            "error": "",
            "exit_code": 0,
            "error_type": "",
            "recoverable": True,
        }

    def _log_detail_result(
        self,
        record: dict,
        dumped: bool,
        dump_path: str | None,
        note: str | None = None,
    ) -> dict:
        """Render a log_detail payload using the peer result-dict convention."""
        payload: dict = {
            "found": True,
            "record": record,
            "dumped": dumped,
            "dump_path": dump_path,
        }
        if note is not None:
            payload["note"] = note
        return {
            "success": True,
            "output": json.dumps(payload, ensure_ascii=False),
            "error": "",
            "exit_code": 0,
            "error_type": "",
            "recoverable": True,
        }

    def _current_vault_secrets(self) -> list[str]:
        """Return the startup secrets plus string values re-read from the vault file now.

        Best-effort on the vault read: an absent or unreadable vault falls back
        to the startup list so log_detail never fails because of the vault.
        """
        from config_schema import parse_vault_content  # noqa: PLC0415 — import-cycle avoidance
        from exceptions import ConfigError  # noqa: PLC0415

        secrets_list = list(getattr(self._owner, "_vault_secrets", []) or [])
        vault_path = getattr(self._owner, "_vault_path", "")
        if not vault_path:
            return secrets_list
        try:
            with open(vault_path, encoding="utf-8") as fh:
                vault = parse_vault_content(
                    fh.read(), vault_path, require_all_strings=False
                )
        except (OSError, ConfigError) as exc:
            logger.warning("log_detail: cannot re-read vault for redaction: %s", exc)
            return secrets_list
        secrets_list.extend(v for v in vault.values() if isinstance(v, str) and v)
        return secrets_list

    def _exec_log_detail(self, args: dict, caller_depth: int = 0, caller_tag: str = "") -> dict:
        """Return one full log record by its store ``id`` at full fidelity.

        Read-only with respect to the SQLite log store; the oversized path may
        write one redacted artifact to the run-scoped results directory. The
        returned record is the same wide-column + extra projection produced by
        :func:`sqlite_log.row_to_record`, including the top-level ``id`` field,
        and is *not* passed through ``_log_query_project`` when it fits within
        the 65536-char cap.

        Args:
            args: Tool arguments; ``id`` (int, required) is the store primary key.
            caller_depth: Accepted for dispatch symmetry with peer handlers.
            caller_tag: Accepted for dispatch symmetry with peer handlers.

        Returns:
            A standard result dict whose ``output`` is a JSON object:
            * found, in cap: ``{"found": True, "record": {...}, "dumped": False,
              "dump_path": None}``
            * found, oversized, dumped: ``{"found": True, "record": <preview>,
              "dumped": True, "dump_path": "..."}``
            * not-found / unreadable store / invalid or out-of-range id:
              ``{"found": False, "record": None, "dumped": False,
              "dump_path": None}``
        """
        raw_id = args.get("id")
        if (
            isinstance(raw_id, bool)
            or not isinstance(raw_id, int)
            or raw_id <= 0
            or raw_id > _SQLITE_INT64_MAX
        ):
            return self._log_detail_empty()

        path = self._owner._log_store_path
        if not path:
            return self._log_detail_empty()

        try:
            conn = connect_store(path)
            conn.execute("PRAGMA busy_timeout = 3000")
        except (OSError, sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
            logger.warning("log_detail: cannot open log store %s: %s", path, exc)
            return self._log_detail_empty()

        try:
            row = conn.execute("SELECT * FROM events WHERE id = ?", (raw_id,)).fetchone()
        except (OSError, sqlite3.Error) as exc:
            logger.warning("log_detail: query failed for store %s: %s", path, exc)
            return self._log_detail_empty()
        finally:
            try:
                conn.close()
            except sqlite3.Error:
                pass

        if row is None:
            return self._log_detail_empty()

        # Trust model (design D5), two layers: (1) structured records are
        # vault-redacted at ingestion by the shared structlog chain, but that
        # pass only knows the secrets present at process start, so credentials
        # logged before being added to the vault slip through; (2) this
        # read-time pass re-redacts the record against the CURRENT vault (file
        # re-read plus the startup list) before anything is returned or dumped.
        # Both the full-fidelity record and the oversized preview/artifact are
        # derived from the redacted record, so they cannot disagree.
        secrets_list = self._current_vault_secrets()
        record = redact_structure(
            row_to_record(row), secrets_list, preserve_top_level_keys=True
        )
        # The 65536-char gate measures the compact serialization; the emitted
        # payload uses default (spaced) separators, so a record just under the
        # gate serializes slightly larger in the tool output. The cap is a
        # fidelity threshold, not an output budget.
        serialized = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        if len(serialized) <= _LOG_DETAIL_MAX_CHARS:
            return self._log_detail_result(record, dumped=False, dump_path=None)

        # Oversized: project a preview, dump the full record to results dir.
        preview = _log_query_project(record)
        results_dir = getattr(self._owner._paths, "results_dir", "")
        if not results_dir:
            note = (
                "Full record exceeds the 65536-character cap but no results "
                "directory is configured; returning a projected preview only."
            )
            return self._log_detail_result(
                preview, dumped=False, dump_path=None, note=note
            )

        # Trace selection: prefer the record's own trace if it matches the
        # canonical trace-id format; otherwise fall back to the current run's
        # trace context; if that also fails, generate a fresh trace id.
        trace_val = record.get("trace")
        trace_id = ""
        if isinstance(trace_val, str) and _TRACE_ID_RE.fullmatch(trace_val):
            trace_id = trace_val
        if not trace_id:
            trace_id = str(structlog.contextvars.get_contextvars().get("trace", "") or "")
        if not _TRACE_ID_RE.fullmatch(trace_id):
            # Local import mirrors the project pattern for trace helpers.
            from trace_context import new_trace_id
            trace_id = new_trace_id()

        dest_dir = os.path.join(results_dir, trace_id)

        # Containment check: the resolved destination must stay within results_dir
        # before any filesystem mutation.
        resolved = os.path.realpath(dest_dir)
        results_real = os.path.realpath(results_dir)
        sep = os.sep
        if not (resolved == results_real or resolved.startswith(results_real + sep)):
            logger.warning(
                "log_detail: dump directory %r is not contained within results_dir %r",
                resolved, results_real,
            )
            note = (
                "Full record exceeds the 65536-character cap but the dump "
                "path is outside the results directory; returning a projected "
                "preview only."
            )
            return self._log_detail_result(
                preview, dumped=False, dump_path=None, note=note
            )

        try:
            os.makedirs(dest_dir, mode=0o700, exist_ok=True)
            os.chmod(dest_dir, 0o700)
        except OSError as exc:
            logger.warning("log_detail: cannot create dump directory %s: %s", dest_dir, exc)
            note = (
                f"Full record exceeds the 65536-character cap but the dump "
                f"directory could not be created ({exc}); returning a "
                f"projected preview only."
            )
            return self._log_detail_result(
                preview, dumped=False, dump_path=None, note=note
            )

        # The artifact is serialized from the structurally redacted record
        # above; no text-level pass runs over the JSON, which would corrupt
        # syntax (numbers, quotes) and merge keys for short secrets.

        base_path = os.path.join(dest_dir, f"log-detail-{raw_id}.json")

        # Records are immutable (append-only store), so a repeated call for the
        # same id produces byte-identical content. Reuse the existing artifact
        # instead of allocating an unbounded collision suffix per call; divergent
        # content (e.g. a changed vault-secrets list) still falls through to the
        # collision-counter path and never clobbers.
        if os.path.exists(base_path):
            try:
                with open(base_path, encoding="utf-8") as fh:
                    if fh.read() == serialized:
                        return self._log_detail_result(
                            preview, dumped=True, dump_path=os.path.abspath(base_path)
                        )
            except OSError:
                pass  # unreadable existing artifact — fall through and rewrite via suffix

        final_path = base_path
        counter = 1
        while os.path.exists(final_path):
            base, ext = os.path.splitext(base_path)
            final_path = f"{base}-{counter}{ext}"
            counter += 1

        fd: int | None = None
        tmp_path: str | None = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=dest_dir,
                prefix=f".{os.path.basename(final_path)}.",
                suffix=".tmp",
                text=True,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fd = None
                fh.write(serialized)
            os.replace(tmp_path, final_path)
            abs_path = os.path.abspath(final_path)
            return self._log_detail_result(preview, dumped=True, dump_path=abs_path)
        except (OSError, ValueError) as exc:
            logger.warning("log_detail: cannot write dump %s: %s", final_path, exc)
            note = (
                f"Full record exceeds the 65536-character cap but the dump "
                f"could not be written ({exc}); returning a projected preview only."
            )
            return self._log_detail_result(
                preview, dumped=False, dump_path=None, note=note
            )
        finally:
            try:
                if fd is not None:
                    os.close(fd)
                if tmp_path is not None and os.path.lexists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass

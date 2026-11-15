"""
scheduler.py
------------
Background task scheduler backed by scheduler.toml and a read-only legacy JSON file.
Runs in a daemon thread so it does not block the Telegram bot.

Job sources:
  - scheduler.toml         writable job definitions; every add/modify/remove/
                           pause/resume is saved here
  - scheduler_jobs.json    legacy job file (XDG state home): only its existence
                           is checked (a notice); it is never read, loaded, run,
                           written, renamed, or deleted

Scheduling uses cron expressions (5-field: minute hour day month weekday).
Old-style scheduler.toml fields (schedule=daily/interval, time, hours, minutes)
leave the job unscheduled with rewrite guidance; no cron is derived or written.

Examples:
  cron = "0 2 * * *"     → daily at 02:00
  cron = "0 */6 * * *"   → every 6 hours (00:00, 06:00, 12:00, 18:00)
  cron = "*/30 * * * *"  → every 30 minutes
"""

from __future__ import annotations

import copy
import hashlib
import html as _html
import json
import logging
import math
import os
import random
import re
import shutil
import threading
import uuid
from dataclasses import dataclass
from datetime import date as _date, datetime, time as _time, timedelta, timezone
from typing import TYPE_CHECKING, Callable, Literal, Optional

import schedule
from croniter import croniter, CroniterBadCronError

from model_registry import bounded_selector_evidence
from sub_agent_registry import SOURCE_SCHEDULED
from sub_agent_supervisor import CANCELLED_SENTINEL, RunCompletion, SupervisionOptions
from telegram_formatter import (
    ResultNotice,
    build_run_result_notice,
    build_unretained_notice,
)

if TYPE_CHECKING:
    from xdg import XDGPaths

logger = logging.getLogger(__name__)

# Sentinel prefix used by legacy agent_fn to signal execution failure.
_ERROR_SENTINEL = "❌"


def _local_int_setting(section_cfg: dict, section: str, key: str, default: int) -> int:
    """Return a scheduler-local integer setting, containing a malformed value.

    The raw value must be an exact ``int`` (a ``bool`` is rejected); nothing is
    coerced with ``int()``. An absent key yields *default*; any other type logs
    a warning naming the setting path (never the raw value) and yields
    *default*, so a bad setting cannot escape Scheduler construction.
    """
    value = section_cfg.get(key, default)
    if type(value) is int:
        return value
    logger.warning(
        "Scheduler setting %s.%s must be an integer (got %s); using default %d. "
        "Fix the value in the config file and restart.",
        section, key, type(value).__name__, default,
    )
    return default


def _classify_job_failure(result: str) -> str:
    """Classify a scheduled job sub-agent result string into an error type.

    Returns an empty string if the result is not a failure.
    """
    if not result:
        return ""
    if not result.startswith(_ERROR_SENTINEL):
        return ""
    # Inspect for known error type strings
    lower = result.lower()
    if "timeout" in lower or "timeoutexception" in lower:
        return "timeout"
    if "ratelimit" in lower or "rate_limit" in lower or "429" in lower:
        return "rate_limit"
    if "connect" in lower or "connection" in lower:
        return "connection"
    if "quota" in lower:
        return "quota"
    if "context" in lower and ("overflow" in lower or "too long" in lower):
        return "context"
    return "unknown"


def _classify_outcome(result: str, success: bool, meta: dict) -> tuple[str, str, str]:
    """Return ``(status, error_type, next_run)`` for a scheduled run's outcome.

    A supervisor "done" result that starts with ``❌`` is a failure; any other
    unsuccessful result is classified as if it carried the sentinel. Failures
    carry the error type and next scheduled run; success and cancellation do not.
    """
    text = result or ""
    if success and not text.startswith(_ERROR_SENTINEL):
        return "done", "", ""
    if not success and text == CANCELLED_SENTINEL:
        return "cancelled", "", ""
    error_type = _classify_job_failure(
        text if text.startswith(_ERROR_SENTINEL) else f"{_ERROR_SENTINEL} {text}"
    )
    return "failed", error_type, _next_run_label(meta)


def _normalize_tag(s: str) -> str:
    """Normalize a tag by lowercasing and collapsing separators to underscores.

    Replaces hyphens, spaces, and dots with underscores, collapses repeated
    underscores, and strips leading/trailing underscores.
    """
    s = s.strip().lower().replace("-", "_").replace(" ", "_").replace(".", "_")
    return re.sub(r"_+", "_", s).strip("_")


def _normalize_context_key(tag: str) -> str:
    """Convert a scheduler tag into a safe spawn_agent context_key.

    The result is bounded to 128 chars to satisfy
    ``builtin_executor._validate_context_key``; otherwise a very long job tag
    would fail validation and prevent the whole scheduled job from running.
    """
    key = tag.strip().lower().replace("-", "_").replace(" ", "_").replace(".", "_")
    key = re.sub(r"[^a-z0-9_]+", "_", key)
    key = _normalize_tag(key)
    key = key[:128].rstrip("_")
    return key or "scheduled_job"

try:
    import tomli
except ImportError:
    import tomllib as tomli  # Python 3.11+


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _legacy_to_cron(stype: str, time_str: Optional[str] = None, hours=None, minutes=None) -> Optional[str]:
    """Convert old schedule_type fields to a cron expression. Returns None for 'once'."""
    if stype == "once":
        return None  # handled separately
    if stype == "daily":
        t = time_str or "02:00"
        try:
            h, m = t.split(":")
            return f"{int(m)} {int(h)} * * *"
        except (ValueError, TypeError):
            return "0 2 * * *"
    if stype == "interval":
        if hours:
            h = int(hours)
            if h == 1:
                return "0 * * * *"
            return f"0 */{h} * * *"
        if minutes:
            m = int(minutes)
            if m == 1:
                return "* * * * *"
            return f"*/{m} * * * *"
    return None


# ---------------------------------------------------------------------------
# Scheduler file load result and type-strict job classification (ADR-0037)
# ---------------------------------------------------------------------------

LoadStatus = Literal["valid", "missing", "unreadable", "unparseable"]

_FILE_FAILURES = {
    "unreadable": "it could not be read",
    "unparseable": "it is not valid TOML",
}


@dataclass(frozen=True)
class SchedulerLoadResult:
    """Outcome of reading the scheduler file.

    ``status`` is ``valid`` (a parsed file, including one with zero jobs),
    ``missing``, ``unreadable`` or ``unparseable``. ``document`` is the parsed
    raw TOML document — the authoritative persisted job configuration — when
    one was parsed, and ``content_hash`` is the SHA-256 of the exact bytes
    read. ``notice`` is the actionable operator notice for an unreadable or
    unparseable file (path, what failed, what to do); it never contains file
    content or parser detail.
    """

    status: LoadStatus
    path: str
    document: Optional[dict] = None
    content_hash: Optional[str] = None
    notice: Optional[str] = None


def _file_failure_notice(path: str, status: str) -> str:
    """Return the operator notice for a scheduler file that failed to load."""
    return (
        f"Scheduler file {path} is {status}: {_FILE_FAILURES[status]}. "
        "No jobs from it are loaded and schedule changes are refused until it "
        "is fixed; the file is left unchanged. Repair or restore the file "
        f"(backups: {os.path.basename(path)}.bak.*), then reload the scheduler "
        "with /jobs reload or restart the agent."
    )


def _read_scheduler_file(path: str) -> SchedulerLoadResult:
    """Read and parse the scheduler file once, classifying the outcome."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return SchedulerLoadResult("missing", path)
    except OSError as exc:
        logger.warning("Could not read scheduler file %s (%s)", path, type(exc).__name__)
        return SchedulerLoadResult(
            "unreadable", path, notice=_file_failure_notice(path, "unreadable"),
        )
    content_hash = hashlib.sha256(raw).hexdigest()
    try:
        document = tomli.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomli.TOMLDecodeError) as exc:
        logger.warning(
            "Could not parse scheduler file %s (%s at line %s)",
            path, type(exc).__name__, getattr(exc, "lineno", "?"),
        )
        return SchedulerLoadResult(
            "unparseable", path, content_hash=content_hash,
            notice=_file_failure_notice(path, "unparseable"),
        )
    return SchedulerLoadResult("valid", path, document=document, content_hash=content_hash)


# Fingerprint of an input file that does not exist; never equal to a SHA-256 digest.
_INPUT_MISSING = "missing"


def _hash_input(raw: Optional[bytes]) -> str:
    """Return the fingerprint of raw input bytes (None means the file is missing)."""
    return _INPUT_MISSING if raw is None else hashlib.sha256(raw).hexdigest()


def _input_fingerprint(path: str) -> Optional[str]:
    """Return the fingerprint of *path*'s current raw bytes without parsing them.

    The SHA-256 of the bytes, ``_INPUT_MISSING`` when the file is absent, or
    None when it cannot be read (so it matches no candidate). Takes no lock.
    """
    try:
        with open(path, "rb") as f:
            return _hash_input(f.read())
    except FileNotFoundError:
        return _INPUT_MISSING
    except OSError:
        return None


# Appended to the returned schedule prose of a job read from the legacy JSON file.
_LEGACY_MARKER = "legacy, read-only"


def _legacy_file_notice(path: str) -> str:
    """Return the operator notice for an existing state-home legacy job file.

    The file is only ever detected, never opened: the notice names its path
    and the manual conversion into ``scheduler.toml`` jobs and carries no
    file content.
    """
    return (
        f"Legacy job file {path} exists but is not loaded: its jobs are not listed, "
        "scheduled or run. To keep a job, recreate it by hand as a `cron` or `once` "
        "job in scheduler.toml (/jobs reload applies it). The file is left unchanged; "
        "you can delete it once its jobs are converted."
    )


# ---------------------------------------------------------------------------
# Scheduler-local TOML encoder (ADR-0037)
# ---------------------------------------------------------------------------
# Re-emits exactly the value types ``tomli`` returns so an explicit patch of
# the parsed raw document keeps every untouched value. Only parsed-value
# equality is promised: comments and layout are not preserved.

_BARE_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")
_TOML_SHORT_ESCAPES = {
    '"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t",
    "\n": "\\n", "\f": "\\f", "\r": "\\r",
}


def _toml_string(value: str, where: str) -> str:
    """Return *value* as a single-line TOML basic string."""
    out = ['"']
    for ch in value:
        code = ord(ch)
        if ch in _TOML_SHORT_ESCAPES:
            out.append(_TOML_SHORT_ESCAPES[ch])
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\u{code:04x}")
        elif 0xD800 <= code <= 0xDFFF:
            raise ValueError(f"{where}: a string contains a lone surrogate")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _toml_key(key: object, where: str) -> str:
    """Return *key* as a bare or quoted TOML key; only string keys are allowed."""
    if type(key) is not str:
        raise ValueError(f"{where}: table keys must be strings, not {type(key).__name__}")
    if _BARE_KEY_RE.fullmatch(key):
        return key
    return _toml_string(key, where)


def _toml_inline(value: object, where: str) -> str:
    """Return *value* as an inline TOML value, refusing anything tomli cannot return."""
    kind = type(value)
    if kind is str:
        return _toml_string(value, where)
    if kind is bool:
        return "true" if value else "false"
    if kind is int:
        return str(value)
    if kind is float:
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return repr(value)
    if kind is datetime:
        offset = value.utcoffset()
        if offset is not None and (offset.seconds % 60 or offset.microseconds):
            raise ValueError(f"{where}: a datetime offset must be whole minutes")
        return value.isoformat()
    if kind is _date:
        return value.isoformat()
    if kind is _time:
        if value.tzinfo is not None:
            raise ValueError(f"{where}: a time of day cannot carry a timezone")
        return value.isoformat()
    if kind is list:
        items = [_toml_inline(item, f"{where}[{i}]") for i, item in enumerate(value)]
        return "[" + ", ".join(items) + "]"
    if kind is dict:
        pairs = [
            f"{_toml_key(k, where)} = {_toml_inline(v, f'{where}.{k}')}"
            for k, v in value.items()
        ]
        return "{" + ", ".join(pairs) + "}" if pairs else "{}"
    if value is None:
        raise ValueError(f"{where}: TOML has no null value")
    raise ValueError(f"{where}: {kind.__name__} values cannot be stored in TOML")


def _emit_toml_table(lines: list[str], path: tuple[str, ...], table: dict) -> None:
    """Append *table* as a standard table: plain values first, then sub-tables."""
    where = ".".join(path) or "<document>"
    for key in table:
        _toml_key(key, where)
    children = [(k, v) for k, v in table.items() if type(v) is dict]
    values = [(k, v) for k, v in table.items() if type(v) is not dict]
    if path and (values or not children):
        if lines:
            lines.append("\n")
        lines.append("[" + ".".join(_toml_key(p, where) for p in path) + "]\n")
    for key, value in values:
        lines.append(f"{_toml_key(key, where)} = {_toml_inline(value, f'{where}.{key}')}\n")
    for key, child in children:
        _emit_toml_table(lines, path + (key,), child)


def _encode_toml_document(document: dict) -> str:
    """Encode a parsed scheduler document as TOML text.

    Covers exactly the types ``tomli`` returns (strings, booleans, integers,
    floats including ``inf``/``nan``, dates, local times, local and offset
    datetimes, arrays, tables, quoted keys). Raises ``ValueError`` naming the
    location and type, never the value, for anything else: arbitrary
    objects, ``None`` anywhere (TOML has no null), and non-string keys.
    """
    if type(document) is not dict:
        raise ValueError(f"<document>: must be a table, not {type(document).__name__}")
    lines: list[str] = []
    _emit_toml_table(lines, (), document)
    return "".join(lines)


def _toml_values_equal(left: object, right: object) -> bool:
    """Parsed-value equality: type-strict (a boolean is not an integer), NaN-aware."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _toml_values_equal(left[k], right[k]) for k in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(map(_toml_values_equal, left, right))
    if isinstance(left, float):
        if math.isnan(left) or math.isnan(right):
            return math.isnan(left) and math.isnan(right)
        return left == right and math.copysign(1.0, left) == math.copysign(1.0, right)
    if isinstance(left, datetime):
        return (
            left.utcoffset() == right.utcoffset()
            and left.replace(tzinfo=None) == right.replace(tzinfo=None)
        )
    return left == right


def _job_tables(document: dict) -> dict:
    """Return the document's ``jobs`` table, creating it when absent."""
    jobs = document.setdefault("jobs", {})
    if type(jobs) is not dict:
        raise ValueError("'jobs' in the scheduler file is not a table")
    return jobs


_SCHEDULER_FILE_HEADER = (
    "# scheduler.toml — auto-managed by scheduler\n"
    "# Edit this file to add static jobs; dynamic/user jobs are appended here.\n"
    "# Comments and layout are not preserved when the scheduler saves a change.\n"
    "# Schedules use cron expressions (5-field, local server time):\n"
    "#   minute hour day month weekday\n"
    "#   Examples: '0 2 * * *' = daily at 02:00, '0 */6 * * *' = every 6h\n"
    "\n"
)


_SCHEDULE_TYPES = ("cron", "once", "daily", "interval")
# Accepted by the ``schedule`` library's daily ``at()`` trigger.
_RUN_AT_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?")
# Legacy ``daily`` time field, as ``_legacy_to_cron`` splits it ("2:3" is "02:03").
_DAILY_TIME_RE = re.compile(r"([01]?\d|2[0-3]):[0-5]?\d")


# Obsolete job fields (ADR-0044): never converted, derived or ignored; they become per-job config errors.
_OBSOLETE_SCHEDULE_TYPES = ("daily", "interval")
_OBSOLETE_SCHEDULE_FIELDS = ("time", "hours", "minutes")


class _OmittedLegacyField:
    """Distinguish an omitted add argument from an explicitly supplied null."""


_OMITTED_LEGACY_FIELD = _OmittedLegacyField()
_OBSOLETE_SCHEDULE_GUIDANCE = (
    'Rewrite the schedule as schedule = "cron" with a cron expression, or schedule = "once" with '
    'run_at = "HH:MM", then reload.'
)
_FALLBACK_MODELS_ERROR = (
    "fallback_models is obsolete and a scheduled job never falls back to another model. "
    "Remove the fallback_models key from this job in scheduler.toml, then reload."
)


def _obsolete_schedule_names(schedule_type: object, present_fields: "list[str]") -> "list[str]":
    """Name the obsolete scheduling settings only (never any raw value)."""
    names = [f'schedule = "{schedule_type}"'] if schedule_type in _OBSOLETE_SCHEDULE_TYPES else []
    return names + [field for field in _OBSOLETE_SCHEDULE_FIELDS if field in present_fields]


def _obsolete_schedule_error(names: "list[str]") -> str:
    """The field-naming, cron/once-guiding config error for obsolete scheduling settings."""
    return (
        f"obsolete scheduling setting(s): {', '.join(names)}. They are never converted into a cron "
        f"or ignored. {_OBSOLETE_SCHEDULE_GUIDANCE}"
    )


def obsolete_mutation_error(supplied: "dict") -> Optional[str]:
    """Return refusal guidance when an add/modify supplies an obsolete field, else ``None``.

    Presence decides (any value, including ``None``, ``[]`` or ``False``): ``time``, ``hours``,
    ``minutes``, ``fallback_models`` keys and a ``schedule_type``/``schedule`` of ``daily`` or
    ``interval``. Only field names appear in the message, never values.
    """
    names = _obsolete_schedule_names(
        next((supplied[k] for k in ("schedule_type", "schedule") if supplied.get(k) in _OBSOLETE_SCHEDULE_TYPES), None),
        [field for field in _OBSOLETE_SCHEDULE_FIELDS if field in supplied],
    )
    if names:
        return f"schedule: obsolete input refused, nothing changed: {', '.join(names)}. {_OBSOLETE_SCHEDULE_GUIDANCE}"
    if "fallback_models" in supplied:
        return (
            "schedule: fallback_models is obsolete and refused, nothing changed: scheduled jobs never "
            "fall back to another model. Remove fallback_models from the request."
        )
    return None


def _strict_legacy_cron(stype: str, job_cfg: dict) -> tuple[Optional[str], Optional[str]]:
    """Derive a cron from legacy ``daily``/``interval`` fields without coercion.

    Returns ``(cron, config_error)``; exactly one of them is set.
    """
    if stype == "daily":
        time_str = job_cfg.get("time", "02:00")
        if not isinstance(time_str, str) or not _DAILY_TIME_RE.fullmatch(time_str):
            return None, 'daily jobs need time as "HH:MM"'
        return _legacy_to_cron("daily", time_str=time_str), None
    hours = job_cfg.get("hours")
    minutes = job_cfg.get("minutes")
    for name, value in (("hours", hours), ("minutes", minutes)):
        if value is not None and not (type(value) is int and value >= 0):
            return None, f"interval {name} must be a non-negative integer"
    cron = _legacy_to_cron("interval", hours=hours, minutes=minutes)
    if cron is None:
        return None, "interval jobs need hours or minutes"
    return cron, None


def _classify_schedule(
    job_cfg: dict, *, reject_obsolete: bool = True,
) -> tuple[object, Optional[str], Optional[str], Optional[str], bool]:
    """Type-strictly derive one job's schedule from its raw TOML fields.

    Returns ``(schedule_type, cron, run_at, config_error, migrated)``.
    ``config_error`` is set when the job cannot be scheduled; ``migrated`` is
    True when a legacy ``daily``/``interval`` job derived its cron in memory.
    Nothing is coerced with ``int()`` or ``bool()``. With ``reject_obsolete`` (the TOML path) a
    ``daily``/``interval`` schedule or any present ``time``/``hours``/``minutes`` field is a
    config error and nothing is derived; the legacy JSON reader (task 8.4) passes ``False`` and
    keeps its in-memory derivation until that task is authorized.
    """
    stype = job_cfg.get("schedule", "cron")
    raw_cron = job_cfg.get("cron")
    cron = raw_cron if isinstance(raw_cron, str) else None
    if not isinstance(stype, str) or stype not in _SCHEDULE_TYPES:
        allowed = "cron, once" if reject_obsolete else "cron, once, daily, interval"
        return stype, cron, None, f"schedule must be one of: {allowed}", False
    if reject_obsolete:
        names = _obsolete_schedule_names(stype, [field for field in _OBSOLETE_SCHEDULE_FIELDS if field in job_cfg])
        if names:
            return stype, cron, None, _obsolete_schedule_error(names), False
    if raw_cron is not None and cron is None:
        return stype, None, None, "cron must be a string", False
    if stype == "once":
        run_at = job_cfg.get("run_at")
        if run_at is None:
            run_at = job_cfg.get("time")
        if not isinstance(run_at, str) or not _RUN_AT_RE.fullmatch(run_at):
            return stype, cron, None, 'once jobs need run_at as "HH:MM"', False
        return stype, cron, run_at, None, False
    migrated = False
    if stype in ("daily", "interval") and not cron:
        cron, error = _strict_legacy_cron(stype, job_cfg)
        if error is not None:
            return stype, None, None, error, False
        stype = "cron"
        migrated = True
    if not cron:
        return stype, None, None, "cron is required for cron jobs", False
    try:
        croniter(cron)
    except (CroniterBadCronError, ValueError):
        return stype, cron, None, "cron is not a valid 5-field cron expression", False
    return stype, cron, None, None, migrated


# ---------------------------------------------------------------------------
# JobExecutionLog
# ---------------------------------------------------------------------------

_HISTORY_PERSIST_MARKER = "history persistence failure"  # dedicated last_error marker


def _next_run_label(meta: dict) -> str:
    """Return the job's next-run time for failure metadata, or "unknown" if unset."""
    next_run = meta.get("_next_run")
    return str(next_run) if next_run is not None else "unknown"


def _new_run_id() -> str:
    """Mint an independent scheduled execution ID (not an agent/prompt ID)."""
    return f"run-{uuid.uuid4().hex}"


class JobExecutionLog:
    """Execution log for scheduled jobs, stored as JSONL.

    Each entry:
        {"ts": ISO8601, "tag": str, "task": str, "result": str,
         "success": bool, "elapsed_s": int, "model": str}
    New runs also carry ``run_id`` (``run-<uuid4 hex>``), terminal ``status``
    (done/failed/cancelled) and, for failures, ``error_type``/``next_run``.
    ``result`` is the complete returned text; no per-result size limit.

    Rotation (applied on every ``record()`` call):
    - Drop entries older than ``max_age_hours``
    - Keep at most ``max_per_job`` most recent entries per tag
    """

    def __init__(
        self,
        log_file: str,
        max_age_hours: int = 48,
        max_per_job: int = 10,
    ) -> None:
        self._log_file = log_file
        self._max_age_hours = max_age_hours
        self._max_per_job = max_per_job
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(
        self,
        tag: str,
        task: str,
        result: str,
        success: bool,
        elapsed_s: int = 0,
        model: str = "",
        error_type: str = "",
        run_id: str = "",
        status: str = "",
        next_run: str = "",
    ) -> bool:
        """Append a new entry and rotate old ones.  Thread-safe.

        Returns True when the entry was persisted, False when the write failed.
        """
        entry = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tag": tag,
            "task": (task or "")[:200],
            "result": result or "",
            "success": bool(success),
            "elapsed_s": int(elapsed_s),
            "model": model or "",
        }
        if run_id:
            entry["run_id"] = run_id
        if status:
            entry["status"] = status
        if error_type:
            entry["error_type"] = error_type
        if next_run:
            entry["next_run"] = next_run
        with self._lock:
            entries = self._read_entries()
            if entries is None:
                # Unreadable existing history: never rewrite it from an empty
                # list; report the failure so the persistence marker fires.
                persisted = False
            else:
                entries.append(entry)
                entries = self._rotate(entries)
                persisted = self._write(entries)
        logger.debug(
            "JobExecutionLog: recorded entry for tag=%s success=%s persisted=%s",
            tag, success, persisted,
        )
        return persisted

    def format_for_prompt(self, max_entries: int = 20) -> str:
        """Return a compact human-readable summary for injection into the system prompt."""
        with self._lock:
            entries = self._load()
        if not entries:
            return ""
        # Most recent first, capped
        recent = entries[-max_entries:][::-1]
        lines = ["SCHEDULED JOB EXECUTION HISTORY (most recent first):"]
        for e in recent:
            status = "✅" if e.get("success") else "❌"
            ts = e.get("ts", "?")
            tag = e.get("tag", "?")
            elapsed = e.get("elapsed_s", 0)
            model = e.get("model", "")
            result_preview = (e.get("result") or "").strip()
            if len(result_preview) > 300:
                result_preview = result_preview[:300] + "…"
            model_part = f" [{model}]" if model else ""
            lines.append(f"  {status} {ts} | {tag}{model_part} ({elapsed}s)")
            if result_preview:
                lines.append(f"     → {result_preview}")
        return "\n".join(lines)

    def read_recent(self, n: int = 50) -> list[dict]:
        """Return the most recent ``n`` retained entries (newest last)."""
        return self._load_retained()[-n:]

    def history(self, tag: str) -> list[dict]:
        """Return retained execution summaries for ``tag``, newest first.

        Each summary has ``run_id``, ``status``, ``ts``, ``tag`` and ``legacy``.
        Pre-change records (boolean ``success``, no run ID) are legacy/partial:
        their status maps success=true→done and success=false→failed, and no
        run ID or result text is fabricated for them.
        """
        summaries = []
        for entry in reversed(self._load_retained()):
            if entry.get("tag") != tag:
                continue
            run_id = entry.get("run_id") or None
            legacy = run_id is None
            status = entry.get("status") if not legacy else None
            if not status:
                status = "done" if entry.get("success") else "failed"
            summaries.append({
                "run_id": run_id,
                "status": status,
                "ts": entry.get("ts", ""),
                "tag": tag,
                "legacy": legacy,
            })
        return summaries

    def get_result(self, run_id: str) -> Optional[dict]:
        """Return the complete retained record for ``run_id``, or None.

        Expired, rotated-out, unknown and legacy (ID-less) records are all
        unavailable; read-time retention applies even without a new write.
        """
        if not run_id:
            return None
        for entry in self._load_retained():
            if entry.get("run_id") == run_id:
                return dict(entry)
        return None

    def _load_retained(self) -> list[dict]:
        """Load entries and apply age/per-tag retention at read time."""
        with self._lock:
            entries = self._load()
        return self._rotate(entries)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self) -> list[dict]:
        """Load all entries, treating an unreadable file as empty (reads only).

        Caller must hold self._lock. Writers use ``_read_entries`` instead so a
        read error never rewrites (and wipes) the existing history.
        """
        entries = self._read_entries()
        return entries if entries is not None else []

    def _read_entries(self) -> Optional[list[dict]]:
        """Load all entries; [] when the file is missing, None on a read error.

        Caller must hold self._lock.
        """
        if not os.path.exists(self._log_file):
            return []
        entries = []
        try:
            with open(self._log_file, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        except OSError as exc:
            logger.warning("JobExecutionLog: failed to read %s: %s", self._log_file, exc)
            return None
        return entries

    def _rotate(self, entries: list[dict]) -> list[dict]:
        """Remove stale entries.  Returns a new (filtered) list."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self._max_age_hours)
        cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

        # Drop entries older than max_age_hours
        entries = [e for e in entries if e.get("ts", "") >= cutoff_iso]

        # Per-tag cap: keep only max_per_job most recent per tag
        tag_counts: dict[str, int] = {}
        kept = []
        for e in reversed(entries):
            tag = e.get("tag", "")
            count = tag_counts.get(tag, 0)
            if count < self._max_per_job:
                tag_counts[tag] = count + 1
                kept.append(e)
        kept.reverse()
        return kept

    def _write(self, entries: list[dict]) -> bool:
        """Atomically write entries back to the JSONL file.  Caller must hold self._lock.

        Returns False (after logging a warning) when the write failed.
        """
        tmp_file = self._log_file + ".tmp"
        try:
            with open(tmp_file, "w", encoding="utf-8") as fh:
                for e in entries:
                    fh.write(json.dumps(e, ensure_ascii=False) + "\n")
            os.replace(tmp_file, self._log_file)
        except OSError as exc:
            logger.warning("JobExecutionLog: failed to write %s: %s", self._log_file, exc)
            try:
                os.unlink(tmp_file)
            except OSError:
                pass
            return False
        return True


# ---------------------------------------------------------------------------
# Atomic mutation candidates (ADR-0037 write protocol)
# ---------------------------------------------------------------------------

_RegistrationChange = Literal["keep", "clear", "register"]


class _MutationFailed(Exception):
    """A scheduler mutation was not applied; ``reason`` is operator-safe text.

    The reason names the scheduler file and what to do; it never carries an
    external exception message. Nothing was written or published.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _ReloadRequiredReason(str):
    """A mutation failure reason under the RELOAD_REQUIRED latch (operator-safe text).

    The file may already hold a change (a publication failed after the
    replace), so callers report it as is, never as "was not changed".
    """


@dataclass(frozen=True)
class _MutationPlan:
    """What one mutation changes, built from a detached copy of its job view.

    ``meta`` is the job's new detached runtime view (None removes the job).
    ``patch`` edits a deep copy of the raw document and returns False when
    the file needs no write. ``registration`` says how the job's trigger
    changes; with ``publish_registration`` False a ``register`` trigger is
    prepared (and so validated) but not published.
    """

    meta: Optional[dict]
    patch: Callable[[dict], bool]
    registration: _RegistrationChange = "keep"
    publish_registration: bool = True


@dataclass(frozen=True)
class _PreparedMutation:
    """A fully prepared, unpublished candidate, valid for one configuration generation.

    ``data``/``load_result`` are the exact bytes to write and the matching
    load result to publish (both None when the file needs no write). The
    job's runtime change is an affected-key delta (``meta_set``/``meta_drop``)
    applied to the live view, so concurrent runtime/history updates on that
    view keep their identity; ``new_meta`` is inserted only for a new job and
    ``remove`` drops the job. ``registrations`` are detached ``schedule``
    jobs that replace the tag's registrations when ``replace_registration``.
    """

    generation: int
    data: Optional[bytes]
    load_result: Optional[SchedulerLoadResult]
    new_meta: Optional[dict]
    meta_set: dict
    meta_drop: frozenset
    remove: bool
    replace_registration: bool
    registrations: tuple


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

class Scheduler:
    """
    Manages recurring background tasks loaded from scheduler.toml and dynamic storage.
    `notify_fn` is called with a message string whenever a task with notify=True completes.
    `agent_fn`  is called with a goal string to invoke the agent for scheduled tasks.

    All repeating jobs use cron expressions (local server time).
    One-time jobs use schedule_type='once' with a run_at time string (HH:MM).
    """

    def __init__(
        self,
        config: dict,
        notify_fn: Callable[[str], None],
        paths: "XDGPaths",
        agent_fn: Optional[Callable[[str], str]] = None,
        scheduler_config_path: str = "scheduler.toml",
        builtin_executor=None,
        selector_redactor: Optional[Callable[[str], str]] = None,
    ):
        sched_cfg = config.get("scheduler", {})
        agent_cfg = config.get("agent", {})
        self.enabled: bool = sched_cfg.get("enabled", True)
        self.notify = notify_fn
        self._result_notice_sink: Optional[Callable[[ResultNotice], None]] = None
        self._unretained_run_ids: set[str] = set()
        self.agent = agent_fn
        self.builtin_executor = builtin_executor  # Optional[BuiltinExecutor]
        # Redacts saved selector strings before they reach logs and records.
        self._selector_redactor = selector_redactor
        self._scheduler_config_path = scheduler_config_path
        self._commands_file = str(paths.scheduler_commands)
        self._state_file = str(paths.scheduler_state)
        self._dynamic_jobs_file = str(paths.scheduler_jobs)

        # Execution history log
        _log_max_age = _local_int_setting(
            sched_cfg, "scheduler", "execution_log_max_age_hours", 48,
        )
        _log_max_per = _local_int_setting(
            sched_cfg, "scheduler", "execution_log_max_per_job", 10,
        )
        self.execution_log = JobExecutionLog(
            log_file=str(paths.job_execution_log),
            max_age_hours=_log_max_age,
            max_per_job=_log_max_per,
        )

        # Long-running agent watcher
        self._warn_minutes: int = _local_int_setting(
            agent_cfg, "agent", "long_run_warn_minutes", 30,
        )
        self._warned_agent_ids: set = set()

        self._jobs_meta: dict = {}
        self._run_history: dict = {}  # tag → {last_run, last_error} — persisted forever
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Overlap detection: tracks tags of currently executing jobs
        self._running_jobs: set = set()
        # Admitted agentic runs per tag (run IDs); a tag stays running until
        # every one of its runs has finished, so parallel runs are tracked.
        self._active_runs: dict[str, set[str]] = {}
        # Reentrant lock: guards _running_jobs AND _jobs_meta AND schedule.* calls.
        # RLock needed because _register_job (which acquires) is called from
        # resume_job/reload which already hold the lock.
        self._running_lock = threading.RLock()
        # Serializes all writes to scheduler.toml and scheduler_state.json,
        # preventing race conditions when two jobs finish simultaneously.
        self._save_lock = threading.Lock()
        # Makes "persist one execution record + set/clear its persistence
        # marker" a single atomic step across concurrent run completions.
        # Lock order: _history_lock → _running_lock → _save_lock.
        self._history_lock = threading.RLock()
        # Configuration generation: bumped under _running_lock by every
        # publication of the job map/document (mutation commit, reload), so a
        # mutation candidate prepared outside the locks can detect it is stale.
        self._config_generation = 0

        # Notice from a reload that found the file unreadable/unparseable; the
        # last-good load result, map, and registrations stay published.
        self._reload_failure: Optional[str] = None
        # RELOAD_REQUIRED latch: only a successful reload clears it; a failed
        # reload leaves it exactly as it was.
        self._reload_required = False
        self._load_notice_sent = False
        # Notice that the (never loaded) legacy JSON job file exists: shown to
        # the operator, but it never refuses scheduler.toml changes.
        self._legacy_notice: Optional[str] = None

        try:
            paths.state_home.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "Could not create scheduler state directory %s (%s); "
                "scheduler state may not be saved.",
                paths.state_home, type(exc).__name__,
            )
        self._load_config_jobs(scheduler_config_path, sched_cfg)
        self._legacy_notice = self._detect_legacy_file()  # existence only; never read
        self._load_state()          # overlay last_run/last_error onto active jobs from history
        self._save_state()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._send_load_notice()
        if not self.enabled:
            logger.info("Scheduler is disabled in config.")
            return
        # Registering every job, starting the loop, and bumping the generation
        # are one step under _running_lock: a mutation candidate prepared while
        # the loop was stopped (its trigger left unpublished) commits either
        # before this step, so it is registered here, or after it, so it is
        # stale and re-prepared with its trigger published.
        with self._running_lock:
            for tag, meta in self._jobs_meta.items():
                if meta.get("enabled", True):
                    try:
                        self._register_job(tag, meta)
                    except Exception as exc:
                        # External registration drivers can raise unexpected errors;
                        # contain them per job so startup remains available.
                        self._registration_failed(tag, meta, exc)
            self._thread = threading.Thread(target=self._run_loop, daemon=True, name="scheduler")
            self._thread.start()
            self._config_generation += 1
            count = len(self._jobs_meta)
        logger.info("Scheduler started with %d jobs.", count)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        # The loop is joined outside every lock; clearing the triggers and
        # bumping the generation together invalidates candidates prepared to
        # publish a trigger while the loop was running.
        with self._running_lock:
            schedule.clear()
            self._config_generation += 1
        logger.info("Scheduler stopped.")

    @property
    def selector_redactor(self) -> Optional[Callable[[str], str]]:
        """Return the injected saved-selector redactor (read-only), or None."""
        return self._selector_redactor

    def load_notice(self) -> Optional[str]:
        """Return the actionable scheduler-file notice, or None when the file is usable.

        Set while the scheduler file is known unreadable or unparseable (at
        startup or after a failed reload); a missing file has no notice. The
        RELOAD_REQUIRED latch takes precedence over every file notice. A
        separate notice for an existing (never loaded) legacy JSON file follows
        those; it is informational and never refuses changes by itself.
        """
        notices = [n for n in (self._hard_notice(), self._legacy_notice) if n]
        return "\n".join(notices) or None

    def _hard_notice(self) -> Optional[str]:
        """Return the latch or scheduler-file failure notice that refuses changes, else None."""
        return self.reload_required_notice() or self._reload_failure or self._load_result.notice

    def reload_required_notice(self) -> Optional[str]:
        """Return the reload-required notice while the latch is set, else None (read-only)."""
        if not self._reload_required:
            return None
        return str(self._reload_required_reason())

    def _reload_required_reason(self) -> _ReloadRequiredReason:
        """Return the safe failure for a change refused or left unpublished by the latch."""
        return _ReloadRequiredReason(
            f"{self._scheduler_config_path} may already contain the change; a reload "
            "is required (/jobs reload) before further changes."
        )

    def _quiesce_registrations_locked(self) -> None:
        """Set the RELOAD_REQUIRED latch first, then best-effort clear automatic triggers.

        Caller holds ``_running_lock``. The default scheduler's job list is
        emptied in place (``list.clear``: no slice assignment, alias-safe);
        a failure to clear is logged by class only and contained.
        """
        self._reload_required = True
        try:
            list.clear(schedule.default_scheduler.jobs)
        except Exception as exc:  # noqa: BLE001 - external job list; quiescing is best effort
            logger.warning(
                "Could not clear the scheduled triggers (%s); automatic runs stay "
                "refused until a reload.", type(exc).__name__,
            )

    def _send_load_notice(self) -> None:
        """Send the startup file notice once through the existing notify path."""
        notice = self.load_notice()
        if notice is None or self._load_notice_sent:
            return
        self._load_notice_sent = True
        try:
            self.notify(f"⚠️ {_html.escape(notice)}")
        except Exception as exc:  # noqa: BLE001 - external notify boundary must not block startup
            logger.warning("Could not send the scheduler file notice: %s", type(exc).__name__)

    def _file_refusal(self) -> Optional[dict]:
        """Return a refusal result while latched or the scheduler file is known bad, else None.

        A legacy JSON file notice alone never refuses: only the latch and
        scheduler.toml load/reload failures do.
        """
        notice = self._hard_notice()
        if notice is None:
            return None
        return {"success": False, "error": notice}

    def _resolve_tag(self, tag: str) -> Optional[str]:
        """Normalize tag and resolve to a canonical stored key.

        Accepts underscores, hyphens, and spaces interchangeably so that
        'longterm-memory-update', 'longterm memory update', and
        'longterm_memory_update' all resolve to the same stored key.
        Returns the canonical tag string or None if not found.
        """
        if not tag:
            return None
        # Exact match first
        if tag in self._jobs_meta:
            return tag
        # Normalize: strip, lowercase, collapse spaces/hyphens/dots to underscores
        normalized = _normalize_tag(tag)
        if normalized in self._jobs_meta:
            return normalized
        # Last resort: compare normalized versions of all stored keys
        for stored in self._jobs_meta:
            stored_norm = _normalize_tag(stored)
            if stored_norm == normalized:
                return stored
        return None

    def _resolve_schedule(
        self,
        schedule_type: str,
        cron: Optional[str],
        run_at: Optional[str],
    ) -> tuple[Optional[str], str, Optional[str]]:
        """Resolve the cron expression and effective run_at for a new job.

        Obsolete ``daily``/``interval``/``time``/``hours``/``minutes`` input is refused by the
        callers before this point and is never converted here.

        Returns ``(expr, schedule_type, effective_run_at)`` or raises
        ``ValueError`` with a user-facing message on invalid input.
        """
        if cron:
            expr = cron.strip()
            schedule_type = "cron"
        elif schedule_type == "once":
            expr = None  # once jobs don't use cron
        else:
            raise ValueError(
                "Could not determine schedule. Provide a 'cron' expression "
                "(e.g. '0 */6 * * *') or schedule_type 'once' with run_at (HH:MM)."
            )

        # Validate cron expression
        if expr:
            try:
                croniter(expr)
            except (CroniterBadCronError, ValueError) as exc:
                raise ValueError(f"Invalid cron expression '{expr}': {exc}") from exc

        if schedule_type == "once" and not run_at:
            raise ValueError("'run_at' (HH:MM) is required for once jobs")

        return expr, schedule_type, run_at

    @staticmethod
    def _build_job_meta(
        tag: str,
        task: str,
        schedule_type: str,
        expr: Optional[str],
        effective_run_at: Optional[str],
        notify: bool,
        source: str,
        model: Optional[str],
        preserve_context: bool,
        context_max_messages: int,
        overlap_policy: str,
    ) -> dict:
        """Build the meta dict for a new job (scheduler.toml origin, never legacy)."""
        meta = {
            "tag": tag,
            "task": task,
            "schedule_type": schedule_type,
            "cron": expr,
            "run_at": effective_run_at if schedule_type == "once" else None,
            "notify": notify,
            "enabled": True,
            "source": source,
            "last_run": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "_legacy_read_only": False,
        }
        if model is not None:
            meta["model"] = model
        if preserve_context:
            meta["preserve_context"] = preserve_context
            meta["context_max_messages"] = context_max_messages
        if overlap_policy != "skip":
            meta["overlap_policy"] = overlap_policy
        return meta

    def add_job(
        self,
        tag: str,
        schedule_type: str,
        task: str,
        notify: bool = True,
        hours: Optional[int] | _OmittedLegacyField = _OMITTED_LEGACY_FIELD,
        minutes: Optional[int] | _OmittedLegacyField = _OMITTED_LEGACY_FIELD,
        time_str: Optional[str] | _OmittedLegacyField = _OMITTED_LEGACY_FIELD,
        run_at: Optional[str] = None,
        cron: Optional[str] = None,
        source: str = "dynamic",
        model: Optional[str] = None,
        preserve_context: bool = False,
        context_max_messages: int = 50,
        overlap_policy: str = "skip",
        *,
        _on_attempt: Optional[Callable[[], None]] = None,
    ) -> dict:
        """Add a job, refusing supplied obsolete fields before the attempt hook.

        ``hours``, ``minutes`` and ``time_str`` may be omitted; explicitly
        supplying any of them, including ``None``, is a validation refusal.
        """
        # A known-bad scheduler file is never overwritten: refuse before the attempt.
        refusal = self._file_refusal()
        if refusal is not None:
            return refusal
        # Obsolete scheduling input is known-bad: refuse before any coercion or the attempt hook.
        obsolete = obsolete_mutation_error({
            "schedule_type": schedule_type,
            **{
                key: value for key, value in (("hours", hours), ("minutes", minutes), ("time", time_str))
                if value is not _OMITTED_LEGACY_FIELD
            },
        })
        if obsolete is not None:
            return {"success": False, "error": obsolete}
        # Normalize tag to underscore-separated lowercase (TOML-safe bare key)
        tag = _normalize_context_key(tag)
        if tag in self._jobs_meta:
            if _on_attempt is not None:
                _on_attempt()
            return {"success": False, "error": f"Job '{tag}' already exists."}

        # Resolve and validate schedule
        try:
            expr, schedule_type, effective_run_at = self._resolve_schedule(
                schedule_type, cron, run_at,
            )
        except ValueError as exc:
            return {"success": False, "error": str(exc)}

        # The persisted table normalizes the context settings, so check the
        # raw supplied values before that conversion can hide them. The raw
        # preserve_context is checked before its truthiness is ever used.
        try:
            if preserve_context is not None:
                _toml_inline(preserve_context, f"jobs.{tag}.preserve_context")
            if preserve_context and context_max_messages is not None:
                _toml_inline(context_max_messages, f"jobs.{tag}.context_max_messages")
        except ValueError as exc:
            return {"success": False, "error": self._unsavable_error(tag, exc)}

        meta = self._build_job_meta(
            tag, task, schedule_type, expr, effective_run_at, notify, source,
            model, preserve_context, context_max_messages,
            overlap_policy,
        )
        # Representability is known before the attempt: refuse with no change.
        table = self._new_job_table(meta)
        try:
            _toml_inline(table, f"jobs.{tag}")
        except ValueError as exc:
            return {"success": False, "error": self._unsavable_error(tag, exc)}

        if _on_attempt is not None:
            _on_attempt()

        def add_table(document: dict) -> bool:
            _job_tables(document)[tag] = copy.deepcopy(table)
            return True

        def build(base: Optional[dict]) -> _MutationPlan:
            if base is not None:
                raise _MutationFailed(
                    f"a job with this tag was added meanwhile, so "
                    f"{self._scheduler_config_path} was not written. List the jobs and retry."
                )
            # The trigger is always prepared (validated); it is published only
            # while the loop runs, as start() registers every job otherwise.
            # Decided afresh on every (re)preparation; start()/stop() bump the
            # generation, so a decision made across them is re-prepared.
            return _MutationPlan(
                meta=copy.deepcopy(meta), patch=add_table, registration="register",
                publish_registration=self._loop_running(),
            )

        failure = self._commit_mutation(tag, build)
        if failure is not None:
            return {"success": False, "error": self._mutation_error(tag, failure)}
        self._save_state()
        logger.info("Job added: %s (%s)", tag, self._describe_schedule(meta))
        return {"success": True}

    def remove_job(self, tag: str) -> bool:
        """Remove a job; True on success (see ``_change_job`` for the refusals)."""
        return self._change_job("remove", tag)[0]

    def pause_job(self, tag: str) -> bool:
        """Pause a job; True on success (see ``_change_job`` for the refusals)."""
        return self._change_job("pause", tag)[0]

    def resume_job(self, tag: str) -> bool:
        """Resume a job; True on success (see ``_change_job`` for the refusals)."""
        return self._change_job("resume", tag)[0]

    _CHANGE_VERBS = {"remove": "removed", "pause": "paused", "resume": "resumed"}

    def _change_job(self, action: str, tag: str) -> tuple[bool, str, bool]:
        """Remove, pause, or resume one job; return ``(success, error, attempted)``.

        Known before the attempt, with ``attempted`` False: the latch or a
        known-bad scheduler file (its notice), an empty tag (``error`` ""),
        a job read only from the legacy JSON file, and, for a removal, a
        legacy file on disk that defines the tag or cannot be read or parsed.
        A non-empty unknown tag is an attempted failure with ``error`` ""
        (callers render their own not-found text); a failure while committing
        is attempted with an operator-safe ``error``. A removal's single
        legacy preflight fingerprint is rechecked under the commit locks.
        """
        refusal = self._file_refusal()
        if refusal is not None:
            return False, refusal["error"], False
        if not tag.strip():
            return False, "", False
        canonical = self._resolve_tag(tag) or tag
        with self._running_lock:
            meta = self._jobs_meta.get(canonical)
            if meta is None:
                return False, "", True
            if self._legacy_only_locked(canonical, meta):
                return False, self._legacy_job_refusal(canonical), False
        if action == "remove":
            build = self._removal_builder(canonical)
        else:
            build = self._enabled_builder(canonical, action == "resume")
        verb = self._CHANGE_VERBS[action]
        failure = self._commit_mutation(canonical, self._user_change_builder(canonical, build))
        if failure is not None:
            if isinstance(failure, _ReloadRequiredReason):
                logger.warning("Job '%s' change needs a reload: %s", canonical, failure)
            else:
                logger.warning("Job '%s' was not %s: %s", canonical, verb, failure)
            return False, self._mutation_error(canonical, failure), True
        self._save_state()
        logger.info("Job %s: %s", verb, canonical)
        return True, "", True

    def _legacy_only_locked(self, tag: str, meta: dict) -> bool:
        """Return True for a job read from the legacy JSON file that scheduler.toml lacks.

        Caller holds ``_running_lock``. The trusted origin flag set by the
        legacy reader is not proof alone: the tag must also be absent from
        the stored scheduler.toml document published with this view.
        """
        if meta.get("_legacy_read_only") is not True:
            return False
        document = self._load_result.document
        jobs = document.get("jobs") if isinstance(document, dict) else None
        return not (isinstance(jobs, dict) and tag in jobs)

    def _legacy_job_refusal(self, tag: str) -> str:
        """Return the refusal for changing a job that exists only in the legacy JSON file."""
        legacy = self._dynamic_jobs_file
        return (
            f"Job '{tag}' was not changed: it is a {_LEGACY_MARKER} job from the "
            f"legacy job file {legacy}, and {self._scheduler_config_path} was not "
            f"written. To change or remove it, edit {legacy} manually, then reload "
            "the scheduler (/jobs reload)."
        )

    def _user_change_builder(
        self,
        tag: str,
        build: Callable[[Optional[dict]], _MutationPlan],
    ) -> Callable[[Optional[dict]], _MutationPlan]:
        """Wrap an operator change's plan builder with the read-only legacy guards.

        Every (re)preparation checks the job's origin against the same
        snapshot it is prepared from: a job that is now legacy-only fails the
        change (after the attempt, an executed failure).
        """
        def guarded(base: Optional[dict]) -> _MutationPlan:
            plan = build(base)
            legacy_view = base is not None and base.get("_legacy_read_only") is True
            if not legacy_view:
                return plan
            patch = plan.patch

            def checked(document: dict) -> bool:
                if legacy_view and tag not in _job_tables(document):
                    raise _MutationFailed(
                        f"it is now a {_LEGACY_MARKER} job from the legacy job file "
                        f"{self._dynamic_jobs_file}, so {self._scheduler_config_path} "
                        f"was not written. Edit {self._dynamic_jobs_file} manually, "
                        "then reload the scheduler (/jobs reload)."
                    )
                return patch(document)

            return _MutationPlan(
                meta=plan.meta, patch=checked, registration=plan.registration,
                publish_registration=plan.publish_registration,
            )

        return guarded

    def run_now(self, tag: str) -> dict:
        """Trigger a job immediately in a background thread."""
        resolved = self._resolve_tag(tag)
        if not resolved:
            # Build a helpful error listing known tags
            known = ", ".join(self._jobs_meta.keys()) or "none"
            return {"success": False, "error": f"Job '{tag}' not found. Known jobs: {known}"}
        meta = self._jobs_meta.get(resolved) or {}
        config_error = meta.get("config_error")
        if config_error:
            return {
                "success": False,
                "error": (
                    f"Job '{resolved}' is not scheduled and cannot run: config_error: "
                    f"{config_error}. Fix the job in {self._scheduler_config_path}, "
                    "then reload the scheduler."
                ),
            }
        threading.Thread(
            target=self._run_job, kwargs={"tag": resolved, "manual": True}, daemon=True
        ).start()
        logger.info("Job '%s' triggered manually (run_now)", resolved)
        return {"success": True}

    def show_job(self, tag: str) -> Optional[dict]:
        """Return full metadata for a single job, or None when unknown.

        The returned dict contains the complete job field set used by the
        ``show`` tool action: ``tag``, ``task``, ``schedule`` (prose),
        ``cron`` (raw), ``schedule_type``, ``run_at``, ``next_run``,
        ``last_run``, ``last_error``, ``notify``, ``model``,
        ``preserve_context``, ``context_max_messages``, ``overlap_policy``,
        ``source``, ``enabled``, ``is_running``, ``created_at``, and
        ``config_error`` (present only when the job carries a config error;
        :meth:`config_blocks_schedule` tells whether it also leaves the job unscheduled).
        """
        canonical = self._resolve_tag(tag)
        if not canonical:
            return None
        with self._running_lock:
            running = set(self._running_jobs)
        # A once-job may self-remove between _resolve_tag and this read.
        meta = self._jobs_meta.get(canonical)
        if meta is None:
            return None
        job = {
            "tag": canonical,
            "task": meta.get("task", ""),
            "schedule": self._schedule_prose(meta),
            "schedule_type": meta.get("schedule_type", "cron"),
            "cron": meta.get("cron"),
            "run_at": meta.get("run_at") if meta.get("schedule_type") == "once" else None,
            "next_run": meta.get("_next_run"),
            "last_run": meta.get("last_run"),
            "last_error": meta.get("last_error"),
            "notify": meta.get("notify", True),
            "model": meta.get("model"),
            "preserve_context": meta.get("preserve_context", False),
            "context_max_messages": meta.get("context_max_messages", 50),
            "overlap_policy": meta.get("overlap_policy", "skip"),
            "source": meta.get("source", "config"),
            "enabled": meta.get("enabled", True),
            "is_running": canonical in running,
            "created_at": meta.get("created_at"),
        }
        # Only a job with invalid scheduling fields carries config_error, so a
        # healthy job keeps the compact field set.
        if self._visible_config_error(meta):
            job["config_error"] = self._visible_config_error(meta)
        return job

    def modify_job(
        self,
        tag: str,
        updates: dict,
        *,
        _on_attempt: Optional[Callable[[], None]] = None,
    ) -> dict:
        """Partial-patch an existing job through the atomic mutation commit.

        Only the following updatable fields are merged when provided:
        ``task``, ``cron``, ``schedule_type`` + ``run_at``, ``notify``,
        ``model``, ``preserve_context``, ``context_max_messages``, and
        ``overlap_policy``. ``created_at``, ``last_run``, ``last_error``,
        ``source``, and run history are never modified. Schedule changes are
        re-validated through ``_resolve_schedule`` and re-registered. The
        ``tag`` is immutable and ``enabled`` is rejected (use ``pause_job`` /
        ``resume_job`` instead).

        Returns a result dict matching the convention used by ``add_job``.
        """
        # A known-bad scheduler file is never overwritten: refuse before the attempt.
        refusal = self._file_refusal()
        if refusal is not None:
            return refusal
        obsolete = obsolete_mutation_error(updates)
        if obsolete is not None:
            return {"success": False, "error": obsolete}
        canonical = self._resolve_tag(tag) or tag
        with self._running_lock:
            if canonical not in self._jobs_meta:
                known = ", ".join(self._jobs_meta.keys()) or "none"
                if tag.strip() and _on_attempt is not None:
                    _on_attempt()
                return {"success": False, "error": f"Job '{tag}' not found. Known jobs: {known}"}

            if self._legacy_only_locked(canonical, self._jobs_meta[canonical]):
                return {"success": False, "error": self._legacy_job_refusal(canonical)}

            if "enabled" in updates:
                return {
                    "success": False,
                    "error": (
                        "schedule modify: 'enabled' is controlled by the pause and resume "
                        "actions, not modify. Use pause_job or resume_job instead."
                    ),
                }

            updatable_fields = {
                "task",
                "cron",
                "schedule_type",
                "run_at",
                "notify",
                "model",
                "preserve_context",
                "context_max_messages",
                "overlap_policy",
            }
            provided = {k for k in updates if k in updatable_fields and updates[k] is not None}
            if not provided:
                return {
                    "success": False,
                    "error": (
                        "schedule modify: no updatable fields provided. "
                        "Updatable fields: task, cron, schedule_type, run_at, notify, "
                        "model, preserve_context, context_max_messages, overlap_policy."
                    ),
                }

            meta = self._jobs_meta[canonical]
            schedule_fields = {"cron", "schedule_type", "run_at"}
            schedule_changed = bool(provided & schedule_fields)
            # The caller's own fields, kept apart from the canonical triple
            # derived below: every (re)preparation derives the triple again
            # from the then-current job, never from this pre-attempt view.
            requested = dict(updates)
            requested_provided = frozenset(provided)

            # Build merged schedule values for validation when schedule fields
            # are touched. Otherwise keep current values.
            merged_schedule_type = updates.get("schedule_type", meta.get("schedule_type", "cron"))
            merged_cron = updates.get("cron", meta.get("cron"))
            merged_run_at = updates.get("run_at", meta.get("run_at"))

            if schedule_changed:
                try:
                    expr, schedule_type, effective_run_at = self._resolve_schedule(
                        schedule_type=merged_schedule_type,
                        cron=merged_cron,
                        run_at=merged_run_at,
                    )
                except ValueError as exc:
                    return {"success": False, "error": str(exc)}
                if "schedule_type" in updates and updates["schedule_type"] != schedule_type:
                    return {
                        "success": False,
                        "error": (
                            f"schedule modify: cannot set schedule_type to "
                            f"'{updates['schedule_type']}' while the job keeps its "
                            f"'{merged_cron}' cron expression. Converting a cron job "
                            f"to a once job requires remove + add."
                        ),
                    }
                # Commit the validated canonical schedule triple so stored meta
                # and the re-registration stay consistent even when the caller
                # provided only part of the triple (e.g. a new cron on a once
                # job flips the type to 'cron' and clears run_at).
                updates = dict(updates)
                updates["cron"] = expr
                updates["schedule_type"] = schedule_type
                updates["run_at"] = effective_run_at if schedule_type == "once" else None
                provided |= {"cron", "schedule_type", "run_at"}

            # Representability is known before the attempt: refuse with no
            # change. A field-level None is a removal, never a stored value.
            for field in sorted(provided):
                if updates[field] is None:
                    continue
                try:
                    _toml_inline(updates[field], f"jobs.{canonical}.{field}")
                except ValueError as exc:
                    return {"success": False, "error": self._unsavable_error(canonical, exc)}

        if _on_attempt is not None:
            _on_attempt()

        def build(base: Optional[dict]) -> _MutationPlan:
            meta = self._require_job(canonical, base)
            updates, provided = self._modify_fields(canonical, meta, requested, requested_provided)
            for field in provided:
                value = updates[field]
                if field == "context_max_messages":
                    try:
                        value = int(value)
                    except (ValueError, TypeError):
                        value = 50
                elif field == "overlap_policy":
                    value = str(value) if value else "skip"
                elif field == "preserve_context":
                    value = bool(value)
                meta[field] = value

            # Enforce the invariant that context_max_messages is only stored
            # when preserve_context is truthy (mirrors _build_job_meta).
            if not meta.get("preserve_context"):
                meta.pop("context_max_messages", None)

            # Persist only the requested fields (and the context cap they drop).
            fields: dict = {}
            dropped: set = set()
            for field in provided:
                key = "schedule" if field == "schedule_type" else field
                if meta.get(field) is None:
                    dropped.add(key)
                else:
                    fields[key] = meta[field]
            if provided & {"preserve_context", "context_max_messages"} and not meta.get(
                "preserve_context"
            ):
                fields.pop("context_max_messages", None)
                dropped.add("context_max_messages")

            # Re-register only when schedule fields changed AND the job is
            # enabled. pause_job clears the schedule-library registration; a
            # schedule patch on a paused job must not silently re-add it with a
            # fresh _next_run. resume_job remains the path back into the
            # registered state and re-registers with the already-patched meta.
            register = schedule_changed and meta.get("enabled", True)
            return _MutationPlan(
                meta=meta,
                patch=self._job_fields_patch(canonical, fields, dropped),
                registration="register" if register else "keep",
            )

        failure = self._commit_mutation(canonical, self._user_change_builder(canonical, build))
        if failure is not None:
            return {"success": False, "error": self._mutation_error(canonical, failure)}
        self._save_state()
        logger.info("Job modified: %s", canonical)
        return {"success": True}

    def _modify_fields(
        self, tag: str, base: dict, requested: dict, requested_provided: frozenset,
    ) -> tuple[dict, set]:
        """Return the modify's ``(updates, provided)`` against the current *base* view.

        When the caller touched a schedule field, the canonical schedule
        triple is derived from *base* and the caller's own fields only, so a
        schedule committed meanwhile is kept rather than a stale triple
        restored. Raises ``_MutationFailed`` when it no longer validates.
        """
        updates = dict(requested)
        provided = set(requested_provided)
        if not provided & {"cron", "schedule_type", "run_at"}:
            return updates, provided
        try:
            expr, schedule_type, effective_run_at = self._resolve_schedule(
                schedule_type=requested.get("schedule_type", base.get("schedule_type", "cron")),
                cron=requested.get("cron", base.get("cron")),
                run_at=requested.get("run_at", base.get("run_at")),
            )
        except ValueError as exc:
            raise _MutationFailed(
                f"its schedule changed meanwhile and the change no longer validates "
                f"({exc}), so {self._scheduler_config_path} was not written. "
                "Show the job and retry the change."
            ) from exc
        if "schedule_type" in requested and requested["schedule_type"] != schedule_type:
            raise _MutationFailed(
                f"its schedule changed meanwhile to a cron schedule, so it cannot become "
                f"'{requested['schedule_type']}' and {self._scheduler_config_path} was "
                "not written. Converting a cron job to a once job requires remove + add."
            )
        updates["cron"] = expr
        updates["schedule_type"] = schedule_type
        updates["run_at"] = effective_run_at if schedule_type == "once" else None
        provided |= {"cron", "schedule_type", "run_at"}
        return updates, provided

    @staticmethod
    def _visible_config_error(meta: dict) -> Optional[str]:
        """The operator-visible config error: scheduling error and/or the launch-time fallback error."""
        parts = [e for e in (meta.get("config_error"), meta.get("launch_config_error")) if e]
        return "; ".join(parts) or None

    def config_blocks_schedule(self, tag: str) -> bool:
        """True when ``tag``'s config error leaves it unscheduled (a ``fallback_models``-only error does not)."""
        meta = self._jobs_meta.get(self._resolve_tag(tag) or tag) or {}
        return bool(meta.get("config_error"))

    def list_jobs(self) -> list:
        with self._running_lock:
            running = set(self._running_jobs)
        result = []
        for tag, meta in self._jobs_meta.items():
            entry = {
                "tag": tag,
                "schedule": self._schedule_prose(meta),
                "schedule_type": meta.get("schedule_type", "cron"),
                "cron": meta.get("cron"),
                "next_run": meta.get("_next_run"),
                "enabled": meta.get("enabled", True),
                "last_run": meta.get("last_run"),
                "last_error": meta.get("last_error"),
                "task": meta.get("task", ""),
                "notify": meta.get("notify", True),
                "source": meta.get("source", "config"),
                "model": meta.get("model"),
                "preserve_context": meta.get("preserve_context", False),
                "is_running": tag in running,
                "config_error": self._visible_config_error(meta),
            }
            result.append(entry)
        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _schedule_prose(self, meta: dict) -> str:
        """Return the list/show schedule prose; a legacy JSON job carries the read-only marker."""
        prose = self._describe_schedule(meta)
        if meta.get("_legacy_read_only") is True:
            return f"{prose} ({_LEGACY_MARKER})"
        return prose

    def _describe_schedule(self, meta: dict) -> str:
        stype = meta.get("schedule_type", "cron")
        if stype == "once":
            run_at = meta.get("run_at") or meta.get("time")
            return f"once at {run_at}" if run_at else "once (ASAP)"
        expr = meta.get("cron", "")
        if expr:
            # Append a human hint for common patterns
            hints = {
                "0 * * * *": "hourly",
                "0 0 * * *": "daily at 00:00",
                "0 2 * * *": "daily at 02:00",
                "0 3 * * *": "daily at 03:00",
                "0 */6 * * *": "every 6h",
                "0 */4 * * *": "every 4h",
                "0 */12 * * *": "every 12h",
                "*/30 * * * *": "every 30m",
            }
            hint = hints.get(expr)
            return f"cron: {expr}" + (f" ({hint})" if hint else "")
        return "cron (no expression)"

    # Maximum jitter window: ±5 minutes (in seconds)
    _JITTER_MAX_SECS = 5 * 60

    @staticmethod
    def _registration_error(exc: BaseException) -> str:
        """Return the safe, operator-visible error for a failed registration."""
        return (
            f"Registration failed ({type(exc).__name__}). "
            "Check the job schedule and scheduler driver availability, then reload the scheduler."
        )

    def _registration_failed(self, tag: str, meta: dict, exc: Exception) -> None:
        """Disable this runtime registration with a safe, operator-visible error."""
        error = self._registration_error(exc)
        with self._running_lock:
            schedule.clear(tag)
            meta.pop("_next_run", None)
            meta.pop("_natural_next_run", None)
            meta["config_error"] = error
        # Driver exception messages can contain raw input; never include them.
        logger.warning("Job '%s' is not scheduled: %s", tag, error)

    def _register_job(self, tag: str, meta: dict) -> None:
        """Compute and store the next_run timestamp for cron jobs.
        Once jobs are registered with the schedule library (HH:MM trigger).
        A job carrying a ``config_error`` stays visibly unscheduled.
        """
        with self._running_lock:
            schedule.clear(tag)
            if meta.get("config_error"):
                meta.pop("_next_run", None)
                meta.pop("_natural_next_run", None)
                return
            stype = meta.get("schedule_type", "cron")

            if stype == "once":
                run_at = meta.get("run_at") or meta.get("time")
                if run_at:
                    schedule.every().day.at(run_at).do(self._run_job, tag=tag).tag(tag)
                else:
                    schedule.every(1).minutes.do(self._run_job, tag=tag).tag(tag)
                logger.debug("Registered once job: %s at %s", tag, run_at)
                return

        # Cron job — compute next_run using local time
        expr = meta.get("cron")
        if not expr:
            logger.warning("Job '%s' has no cron expression — skipping", tag)
            return
        try:
            now_local = datetime.now()
            cron = croniter(expr, now_local)
            natural_next = cron.get_next(datetime)
            # Store the natural (un-jittered) fire time so the rescheduling base is
            # always at or after the real cron tick — prevents double-execution when
            # negative jitter fires the job before the natural tick time.
            meta["_natural_next_run"] = natural_next.isoformat()
            # Apply ±jitter (capped at _JITTER_MAX_SECS) only for the first run
            jitter_secs = random.randint(-self._JITTER_MAX_SECS, self._JITTER_MAX_SECS)
            next_run = natural_next + timedelta(seconds=jitter_secs)
            meta["_next_run"] = next_run.isoformat()
            sign = "+" if jitter_secs >= 0 else ""
            logger.debug(
                "Registered cron job: %s (%s) → next run %s (jitter %s%ds)",
                tag, expr, next_run.strftime("%Y-%m-%d %H:%M:%S"), sign, jitter_secs,
            )
        except (CroniterBadCronError, ValueError, TypeError) as exc:
            self._registration_failed(tag, meta, exc)

    def _release_run(self, tag: str, run_id: Optional[str] = None) -> None:
        """Release one run of ``tag``; the tag is idle only when no run remains.

        ``run_id`` is None for non-agentic triggers (empty-task reminders),
        which never hold an admitted run. Releasing an already-released run is
        a no-op, so a duplicate finish signal cannot release another run.
        """
        if run_id is not None:
            self._unretained_run_ids.discard(run_id)
        with self._running_lock:
            runs = self._active_runs.get(tag, set())
            if run_id is not None:
                if run_id not in runs:
                    return
                runs.discard(run_id)
            if runs:
                return
            self._active_runs.pop(tag, None)
            self._running_jobs.discard(tag)

    def _remove_once_job(self, tag: str) -> None:
        """Remove a completed once-job: clear from schedule, pop meta, persist state + TOML.

        The removal goes through the atomic mutation commit. If it cannot be
        committed (e.g. the file was edited outside the scheduler), the job is
        still dropped from the running schedule so it never fires again, and
        the file keeps its table until the operator reloads.
        """
        failure = self._commit_mutation(tag, self._removal_builder(tag))
        if failure is not None:
            logger.warning(
                "Completed once-job '%s' was removed from the running schedule only: %s",
                tag, failure,
            )
            with self._running_lock:
                schedule.clear(tag)
                if self._jobs_meta.pop(tag, None) is not None:
                    self._config_generation += 1
        self._save_state()

    def _run_job(self, tag: str, manual: bool = False) -> None:
        """Dispatch a scheduled job execution.

        Thin dispatcher: overlap check → route to the appropriate strategy.
        When ``manual`` is True the enabled check is skipped so run_now can
        fire paused jobs; scheduled firings continue to respect enabled.
        While the RELOAD_REQUIRED latch is set, automatic firings admit
        nothing; already admitted runs and manual run_now are unaffected.
        """
        if not manual:
            with self._running_lock:
                latched = self._reload_required
            if latched:
                logger.warning(
                    "Job %s not started automatically: %s", tag, self._reload_required_reason(),
                )
                return
        meta = self._jobs_meta.get(tag)
        if not meta:
            return
        if not manual and not meta.get("enabled", True):
            return

        # Fire-time type checks: nothing is coerced (int()/bool()); a wrong
        # value is reported as a normal recorded launch failure below.
        raw_task = meta.get("task", "")
        task = raw_task.strip() if isinstance(raw_task, str) else ""
        bad_fields: list[str] = []
        if not isinstance(raw_task, str):
            bad_fields.append("task must be a string")
        if meta.get("launch_config_error"):
            bad_fields.append(meta["launch_config_error"])
        if type(meta.get("notify", True)) is not bool:
            bad_fields.append("notify must be true or false")
        if type(meta.get("preserve_context", False)) is not bool:
            bad_fields.append("preserve_context must be true or false")
        context_max = meta.get("context_max_messages")
        if context_max is not None and type(context_max) is not int:
            bad_fields.append("context_max_messages must be an integer")
        max_iterations = meta.get("max_iterations")
        if max_iterations is not None and type(max_iterations) is not int:
            bad_fields.append("max_iterations must be an integer")

        # Overlap detection and admission happen in one locked step, so a
        # finishing parallel sibling cannot mark the tag idle mid-admission.
        # Admitted agentic runs mint their independent execution ID here;
        # overlap skips and empty-task reminders never receive one. An
        # invalid policy never infers an execution: an active job is
        # conservatively skipped; an idle job is admitted so the failure is
        # recorded under its own run ID.
        overlap_policy = meta.get("overlap_policy", "skip")
        policy_invalid = not (
            isinstance(overlap_policy, str) and overlap_policy in ("skip", "parallel")
        )
        if policy_invalid:
            bad_fields.append("overlap_policy must be 'skip' or 'parallel'")
        run_id: Optional[str] = None
        with self._running_lock:
            if not manual and self._reload_required:
                return  # latched after the early check: still admit nothing
            if tag in self._running_jobs:
                if overlap_policy == "skip" or policy_invalid:
                    if policy_invalid:
                        logger.warning(
                            "Job %s skipped — invalid overlap policy; "
                            "the active run is left running", tag,
                        )
                    else:
                        logger.warning(
                            "Job %s skipped — previous run still in progress (policy: skip)", tag
                        )
                    return
                # else: parallel — allow multiple instances
            if task or bad_fields:
                run_id = _new_run_id()
            self._running_jobs.add(tag)
            if run_id is not None:
                self._active_runs.setdefault(tag, set()).add(run_id)

        # Every present raw value (including ``""`` and falsey non-strings)
        # forwards verbatim for the trusted scheduled admission to classify.
        job_model = meta.get("model")
        # Evidence describes the saved value (``""`` shows as blank).
        selector_evidence = bounded_selector_evidence(
            meta.get("model"), redact=self._selector_redactor
        )
        _log_extra = f" | model={selector_evidence}"
        logger.info("Running scheduled job %s%s", tag, _log_extra)
        now = datetime.now(timezone.utc).isoformat()
        is_once = meta.get("schedule_type") == "once"

        # A wrong-typed field is a normal recorded launch failure: the run
        # was admitted, so last_run is persisted, the failure is recorded
        # under the run ID, the alert goes out regardless of notify, and
        # tracking is released.
        if bad_fields:
            meta["last_run"] = now
            self._run_history.setdefault(tag, {})["last_run"] = now
            self._save_state()
            self._fail_launch(
                tag, meta,
                "invalid job configuration: " + "; ".join(bad_fields),
                "failed to launch",
                task=task, model=selector_evidence, run_id=run_id,
            )
            return

        # --- Empty task guard ---
        if run_id is None:
            self._handle_empty_task(tag, meta, now, is_once)
            return

        # Prefer spawn_agent via builtin_executor if available
        if self.builtin_executor is not None and hasattr(self.builtin_executor, 'spawn_agent'):
            self._run_via_spawn_agent(tag, meta, task, job_model, now, is_once, run_id)
            return

        # Fallback: direct agent_fn call (legacy / no builtin_executor)
        self._run_via_agent_fn(tag, meta, task, job_model, now, is_once, run_id)

    def _run_result_recorder(self, run_id: str, meta: dict) -> Callable[..., None]:
        """Return a per-run result-log callback recording a complete terminal record."""

        def record(
            *, tag: str, task: str, result: str, success: bool,
            elapsed_s: int = 0, model: str = "",
        ) -> None:
            status, error_type, next_run = _classify_outcome(result, success, meta)
            self._record_execution(
                meta,
                tag=tag, task=task, result=result, success=status == "done",
                elapsed_s=elapsed_s, model=model, error_type=error_type,
                run_id=run_id, status=status, next_run=next_run,
            )

        return record

    def set_result_notice_sink(self, sink: Optional[Callable[[ResultNotice], None]]) -> None:
        """Route scheduled run-result notices to ``sink`` instead of ``notify``.

        The Telegram adapter installs its idle queue here. Direct alerts
        (launch failures, empty-task reminders, long-running warnings) never
        use the sink. ``None`` restores direct delivery.
        """
        self._result_notice_sink = sink

    def _deliver_result_notice(self, notice: ResultNotice) -> None:
        """Hand a run-result notice to the sink, or its HTML to ``notify`` when none is set."""
        sink = self._result_notice_sink
        try:
            if sink is not None:
                sink(notice)
            else:
                self.notify(notice.text)
        except Exception as exc:  # noqa: BLE001 - best-effort delivery; history is authoritative
            logger.warning("Could not deliver scheduled run-result notice: %s", exc)

    def _run_completion_handler(
        self, run_id: str, meta: dict,
    ) -> Callable[[RunCompletion], None]:
        """Return the per-run handler that turns a terminal outcome into a notice.

        The supervisor invokes this only for notifying runs that are not
        timeout-cancelled. A run whose outcome could not be persisted gets its
        real outcome summary plus a truthful "not retained" note instead of the
        result and a Details control.
        """

        def complete(completion: RunCompletion) -> None:
            status, error_type, next_run = _classify_outcome(
                completion.result, completion.status == "done", meta,
            )
            if run_id in self._unretained_run_ids:
                self._unretained_run_ids.discard(run_id)
                notice = build_unretained_notice(
                    completion.tag, status, error_type, next_run,
                )
            else:
                notice = build_run_result_notice(
                    completion.tag, status, completion.result, error_type, next_run, run_id,
                )
            self._deliver_result_notice(notice)

        return complete

    def _record_execution(self, meta: dict, **record_kwargs) -> None:
        """Persist one execution record, applying write-failure precedence.

        A failed write always logs the real run outcome, sets the job's
        ``last_error`` to the history-persistence marker (overwriting any prior
        value) and, when ``notify`` is true, flags the run so its completion
        handler sends the real outcome with a "not retained" note. Nothing is
        queued here, so timeout-cancel suppression and direct launch-failure
        alerts stay in charge. A successful write clears ``last_error`` only
        while it still holds that marker.
        """
        tag = record_kwargs["tag"]
        run_id = record_kwargs.get("run_id", "")
        status = record_kwargs.get("status", "")
        with self._history_lock:
            if self.execution_log.record(**record_kwargs):
                self._clear_persistence_marker(tag, meta)
                return
            marker = (
                f"{_HISTORY_PERSIST_MARKER}: outcome of run {run_id or 'unknown'} "
                f"({status or 'unknown'}) was not retained; Details are unavailable."
            )
            logger.warning(
                "Job %s: %s Run outcome: status=%s result=%s",
                tag, marker, status, record_kwargs.get("result", ""),
            )
            self._set_job_last_error(tag, meta, marker)
        if run_id and meta.get("notify", True):
            self._unretained_run_ids.add(run_id)

    def _set_job_last_error(self, tag: str, meta: dict, value: Optional[str]) -> None:
        """Set ``last_error`` on the active job (if any) and retained history.

        Mutations take ``_running_lock`` (guards ``_jobs_meta`` snapshots) and
        ``_save_lock`` (guards serialization of ``_run_history``), in the
        documented order, so they never race an in-progress state save.
        """
        with self._running_lock, self._save_lock:
            targets = [meta]
            active = self._jobs_meta.get(tag)
            if active is not None and active is not meta:
                targets.append(active)
            for target in targets:
                if value is None:
                    target.pop("last_error", None)
                else:
                    target["last_error"] = value
            self._run_history.setdefault(tag, {})["last_error"] = value
        self._save_state()

    def _clear_persistence_marker(self, tag: str, meta: dict) -> None:
        """Clear ``last_error`` only if it still holds the persistence marker."""
        current = self._run_history.get(tag, {}).get("last_error") or ""
        if _HISTORY_PERSIST_MARKER in current:
            self._set_job_last_error(tag, meta, None)

    def _handle_empty_task(self, tag: str, meta: dict, now: str, is_once: bool) -> None:
        """Send a direct notification for jobs with no task text."""
        logger.warning("Job %s: no task — sending direct notification", tag)
        friendly = tag.replace("_", " ")
        meta["last_run"] = now
        self._run_history[tag] = {"last_run": now, "last_error": None}
        self._save_state()
        self._release_run(tag)
        if is_once:
            self._remove_once_job(tag)
        if meta.get("notify", True):
            self.notify(f"🔔 <b>Reminder:</b> {_html.escape(friendly)}")

    def _run_via_spawn_agent(
        self, tag: str, meta: dict, task: str, job_model: Optional[str], now: str, is_once: bool,
        run_id: str,
    ) -> None:
        """Execute a job by spawning a sub-agent via builtin_executor."""
        preserve_ctx = meta.get("preserve_context", False)
        context_key = _normalize_context_key(tag) if preserve_ctx else None

        spawn_args: dict = {"task": task}
        if job_model is not None:
            spawn_args["model"] = job_model
        if context_key:
            # Marker only: the spawn path ignores this value for scheduled runs
            # and derives an origin-scoped key from the job tag instead.
            spawn_args["context_key"] = context_key

        # Internal supervision controls travel through per-submission options,
        # NOT through the model-facing spawn_args dict. Per-submission options
        # avoid the shared-attribute race when multiple jobs fire concurrently.
        options = SupervisionOptions(
            job_tag=tag,
            # finish callback → releases this run when the sub-agent ends
            finish_cb=lambda _finish_tag: self._release_run(tag, run_id),
            # per-run execution-log recorder so the supervisor logs the
            # complete job result under this run's execution ID
            result_log_cb=self._run_result_recorder(run_id, meta),
            # terminal handoff: classified run-result notice for the presentation owner
            completion_cb=self._run_completion_handler(run_id, meta),
            # honour the job's notify setting — False suppresses Telegram output
            notify=meta.get("notify", True),
            # tag the registry record as a scheduled run (internal channel,
            # never a model-facing spawn_agent arg); still capacity-counted.
            source=SOURCE_SCHEDULED,
        )

        # Update last_run before spawning (we know it started)
        meta["last_run"] = now
        self._run_history.setdefault(tag, {})["last_run"] = now
        self._save_state()

        try:
            result = self.builtin_executor.spawn_agent(spawn_args, options=options)
        except Exception as exc:  # noqa: BLE001 - any launch error must release the admitted run
            logger.error("Job %s spawn raised: %s", tag, exc, exc_info=True)
            result = {
                "success": False,
                "error": f"spawn_agent raised {type(exc).__name__}: {exc}",
            }
        if not result.get("success"):
            logger.error("Job %s spawn failed: %s", tag, result.get("error"))
            self._fail_launch(
                tag, meta, str(result.get("error", "spawn failed")), "failed to spawn",
                task=task,
                model=bounded_selector_evidence(meta.get("model"), redact=self._selector_redactor),
                run_id=run_id,
            )
        else:
            # this run is released by its finish_cb when the sub-agent ends
            # Auto-remove once jobs (spawn_agent will still deliver result)
            if is_once:
                self._remove_once_job(tag)

    def _run_via_agent_fn(
        self, tag: str, meta: dict, task: str, job_model: Optional[str], now: str, is_once: bool,
        run_id: str,
    ) -> None:
        """Fail closed when no scheduled-profile executor is available.

        The legacy direct-agent fallback never invokes the singleton operator
        agent for agentic scheduled work: that would run the job with operator
        memory and confirmation authority. The run is reported as a launch
        failure through the direct launch-failure alert policy instead.
        """
        logger.error(
            "Job %s not run: scheduled executor unavailable (fail-closed)", tag,
        )
        meta["last_run"] = now
        self._run_history.setdefault(tag, {})["last_run"] = now
        self._fail_launch(
            tag,
            meta,
            "Scheduled executor unavailable: the job was not run (fail-closed; "
            "scheduled work never uses the operator agent). Check the agent's "
            "startup log for executor wiring errors, restart it, then retry with "
            "run_now.",
            "failed to launch",
            task=task,
            model=bounded_selector_evidence(meta.get("model"), redact=self._selector_redactor),
            run_id=run_id,
        )

    def _fail_launch(
        self, tag: str, meta: dict, error_message: str, headline: str,
        *, task: str, model: str, run_id: str,
    ) -> None:
        """Record a launch failure, alert directly (independent of notify), release the run.

        The admitted run's failed execution record carries its run ID and the
        launch error, with failure classification and next-run metadata. The
        alert repeats the record's bounded, redacted ``model`` evidence
        (computed once by the caller, HTML-escaped here).
        """
        try:
            meta["last_error"] = error_message
            self._run_history.setdefault(tag, {})["last_error"] = error_message
            self._save_state()
            error_type = _classify_job_failure(_ERROR_SENTINEL + " " + error_message)
            next_run = _next_run_label(meta)
            self._record_execution(
                meta,
                tag=tag, task=task, result=error_message, success=False,
                model=model, error_type=error_type, run_id=run_id,
                status="failed", next_run=next_run,
            )
            self.notify(
                f"⚠️ <b>Job {_html.escape(tag)} {headline}</b>"
                f" ({_html.escape(error_type)})\n"
                f"Next run: {_html.escape(next_run)}\n"
                f"Model: {_html.escape(model)}\n"
                f"{_html.escape(error_message)}"
            )
        finally:
            self._release_run(tag, run_id)

    def _process_pending_commands(self) -> None:
        if not os.path.exists(self._commands_file):
            return
        try:
            with open(self._commands_file) as f:
                commands = json.load(f)
            os.remove(self._commands_file)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read scheduler commands: %s", exc)
            return

        if not isinstance(commands, list):
            return

        for cmd in commands:
            action = cmd.get("action", "")
            tag = cmd.get("tag", "")
            try:
                if action == "add":
                    stype = cmd.get("schedule", "interval")
                    legacy_kwargs = {
                        **({"hours": int(cmd["hours"])} if "hours" in cmd else {}),
                        **({"minutes": int(cmd["minutes"])} if "minutes" in cmd else {}),
                        **({"time_str": cmd["time"]} if "time" in cmd else {}),
                    }
                    notify = str(cmd.get("notify", "true")).lower() != "false"
                    res = self.add_job(
                        tag=tag,
                        schedule_type=stype,
                        task=cmd.get("task", ""),
                        notify=notify,
                        **legacy_kwargs,
                    )
                    logger.info("Command add job '%s': %s", tag, res)
                elif action == "remove":
                    self.remove_job(tag)
                elif action == "pause":
                    self.pause_job(tag)
                elif action == "resume":
                    self.resume_job(tag)
                else:
                    logger.warning("Unknown scheduler command action: %s", action)
            except (KeyError, TypeError, ValueError) as exc:
                logger.error("Error processing scheduler command %s: %s", cmd, exc)

    def _save_state(self) -> None:
        # Snapshot _jobs_meta under _running_lock BEFORE acquiring _save_lock.
        # This avoids lock-ordering deadlock: _run_job (called synchronously by
        # schedule.run_pending in the loop thread) may hold _running_lock and
        # need _save_lock via _save_state, while another thread could hold
        # _save_lock and wait for _running_lock.
        with self._running_lock:
            jobs_snapshot = [(tag, dict(meta)) for tag, meta in self._jobs_meta.items()]
        with self._save_lock:
            self._save_state_locked(jobs_snapshot)

    @staticmethod
    def _state_schedule(meta: dict) -> dict:
        """Return the validated schedule scalars of one job for the state file.

        Only a schedule that re-validates from its derived runtime scalars is
        projected; rejected scheduling input becomes an unscheduled placeholder,
        so no raw or unvalidated value reaches the state file. The runtime
        ``meta`` is never modified.
        """
        enabled = meta.get("enabled", True) is True
        stype = meta.get("schedule_type", "cron")
        if isinstance(stype, str) and stype in _SCHEDULE_TYPES:
            if stype == "once":
                run_at = meta.get("run_at")
                if run_at is None:
                    # A once job without a time runs ASAP, unless its time was rejected.
                    if not meta.get("config_error"):
                        return {"enabled": enabled, "schedule_type": "once",
                                "cron": None, "run_at": None}
                else:
                    _, _, run_at, error, _ = _classify_schedule(
                        {"schedule": "once", "run_at": run_at},
                    )
                    if error is None:
                        return {"enabled": enabled, "schedule_type": "once",
                                "cron": None, "run_at": run_at}
            else:
                _, cron, _, error, _ = _classify_schedule(
                    {"schedule": "cron", "cron": meta.get("cron")},
                )
                if error is None:
                    return {"enabled": enabled, "schedule_type": stype,
                            "cron": cron, "run_at": None}
        return {"enabled": False, "schedule_type": "cron", "cron": None, "run_at": None}

    def _save_state_locked(self, jobs_snapshot: list[tuple[str, dict]]) -> None:
        # Merge current job states into run_history so history is never lost
        for tag, meta in jobs_snapshot:
            if meta.get("last_run") or meta.get("last_error"):
                self._run_history[tag] = {
                    "last_run": meta.get("last_run"),
                    "last_error": meta.get("last_error"),
                }
        # Persist only an explicit projection of each job: raw TOML selectors
        # and unvalidated values stay in the runtime model, never in state.
        jobs: dict = {}
        for tag, meta in jobs_snapshot:
            projected = self._state_schedule(meta)
            jobs[tag] = {
                **projected,
                "last_run": meta.get("last_run"),
                "last_error": meta.get("last_error"),
                "schedule_description": self._describe_schedule(projected),
            }
        state = {"jobs": jobs, "history": self._run_history}
        tmp = self._state_file + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, self._state_file)
        except OSError as exc:
            logger.warning("Could not save scheduler state: %s", exc)
        except (TypeError, ValueError) as exc:
            # A job value JSON cannot represent (e.g. a TOML datetime): the
            # state is not saved; the value itself is never logged.
            logger.warning(
                "Could not serialize scheduler state (%s); state not saved.",
                type(exc).__name__,
            )
            try:
                os.remove(tmp)
            except OSError as cleanup_exc:
                logger.debug("Could not remove %s: %s", tmp, cleanup_exc)

    def _load_state(self) -> None:
        """Load run history from state file and overlay last_run/last_error onto active jobs."""
        if not os.path.exists(self._state_file):
            return
        try:
            with open(self._state_file) as f:
                state = json.load(f)
        except (OSError, ValueError) as exc:
            logger.warning("Could not load scheduler state: %s", exc)
            return
        if not isinstance(state, dict):
            logger.warning(
                "Ignoring scheduler state %s: top level is not an object.", self._state_file,
            )
            return

        # Restore run history (all jobs ever run, including removed ones)
        history = state.get("history", {})
        if not isinstance(history, dict):
            logger.warning("Ignoring malformed history in scheduler state %s.", self._state_file)
            history = {}
        self._run_history = {
            tag: entry for tag, entry in history.items() if isinstance(entry, dict)
        }
        if len(self._run_history) != len(history):
            logger.warning(
                "Ignored %d malformed history entries in scheduler state %s.",
                len(history) - len(self._run_history), self._state_file,
            )

        # Fall back to reading history from old-format state (jobs section had last_run)
        old_jobs = state.get("jobs", {})
        if not self._run_history and isinstance(old_jobs, dict):
            for tag, data in old_jobs.items():
                if not isinstance(data, dict):
                    continue
                if data.get("last_run") or data.get("last_error"):
                    self._run_history[tag] = {
                        "last_run": data.get("last_run"),
                        "last_error": data.get("last_error"),
                    }

        # Overlay historical last_run/last_error onto currently active jobs
        for tag, meta in self._jobs_meta.items():
            if tag in self._run_history:
                hist = self._run_history[tag]
                if hist.get("last_run"):
                    meta["last_run"] = hist["last_run"]
                if hist.get("last_error"):
                    meta["last_error"] = hist["last_error"]

        loaded = len(self._run_history)
        if loaded:
            logger.debug("Restored run history for %d jobs from %s", loaded, self._state_file)

    def _detect_legacy_file(self) -> Optional[str]:
        """Return the legacy-file notice when the state-home job file exists, else None.

        The only contact with the legacy file: a metadata existence check.
        It is never opened, read, hashed, written, renamed or deleted.
        """
        if os.path.exists(self._dynamic_jobs_file):
            return _legacy_file_notice(self._dynamic_jobs_file)
        return None

    _MAX_BACKUPS = 5

    def _prune_backups(self) -> None:
        """Keep only the last _MAX_BACKUPS backup files for scheduler.toml."""
        dir_ = os.path.dirname(os.path.abspath(self._scheduler_config_path))
        fname = os.path.basename(self._scheduler_config_path)
        prefix = fname + ".bak."
        try:
            baks = sorted(
                f for f in os.listdir(dir_) if f.startswith(prefix)
            )
            for old in baks[: max(0, len(baks) - self._MAX_BACKUPS)]:
                os.remove(os.path.join(dir_, old))
        except OSError as exc:
            logger.debug("Could not prune scheduler backups: %s", exc)

    def reload(self) -> dict:
        """
        Hot-reload scheduler.toml as one atomic publication.

        The reload reads, hashes, and parses its own input bytes once, then
        builds the complete candidate (job view and every enabled job's
        registrations) outside the commit locks. Under ``_running_lock`` then
        ``_save_lock`` it rechecks that the file still holds exactly those
        bytes (no re-parse) and publishes document, hash, view, registrations,
        and generation together, clearing the RELOAD_REQUIRED latch. Safe to
        call while the scheduler is running.

        Returns {"reloaded": N, "failed": N}; jobs with a ``config_error`` count
        as failed. On failure the last-good map, document, hash, registrations,
        generation, and latch stay untouched, nothing is retried, and the result
        is {"reloaded": 0, "failed": 0, "error": <actionable notice>}: an
        unreadable or unparseable scheduler file also sets the load notice; a
        file changed during the reload asks the operator to reload again. The
        state-home legacy job file is only checked for existence, on every
        reload outcome: its notice is set while it exists and cleared when it
        is gone (a failed reload changes nothing else); it is never read.
        """
        logger.info("Reloading scheduler.toml…")
        path = self._scheduler_config_path
        load_result, candidate, _ = self._read_config_jobs(path)
        if load_result.notice is not None:
            self._reload_failure = load_result.notice
            logger.warning("Scheduler reload failed (%s); last-good jobs kept.", load_result.status)
            self._legacy_notice = self._detect_legacy_file()  # existence only; nothing else changes
            return {"reloaded": 0, "failed": 0, "error": load_result.notice}
        toml_fp = load_result.content_hash or _INPUT_MISSING
        registrations, reloaded, failed = self._prepare_reload_registrations(candidate)

        publish_fault: Optional[str] = None
        with self._running_lock, self._save_lock:
            toml_now = _input_fingerprint(path)
            if toml_now == toml_fp:
                # Re-overlay run history so last_run/last_error survive a reload.
                for tag, meta in candidate.items():
                    hist = self._run_history.get(tag)
                    if hist:
                        if hist.get("last_run"):
                            meta["last_run"] = hist["last_run"]
                        if hist.get("last_error"):
                            meta["last_error"] = hist["last_error"]
                # The registrations are the only fallible publication step, so
                # they go first: a fault publishes nothing else and keeps (or
                # sets) the latch with the triggers quiesced.
                try:
                    schedule.default_scheduler.jobs[:] = registrations
                except Exception as exc:  # noqa: BLE001 - external job list; contained under the latch
                    self._quiesce_registrations_locked()
                    publish_fault = type(exc).__name__
                except BaseException:
                    self._quiesce_registrations_locked()
                    raise
                else:
                    self._jobs_meta = candidate
                    self._load_result = load_result
                    self._reload_failure = None
                    # Existence only: set while the legacy file exists, cleared when gone.
                    self._legacy_notice = self._detect_legacy_file()
                    self._reload_required = False
                    self._config_generation += 1
                published = True
            else:
                published = False
        if publish_fault is not None:
            self._legacy_notice = self._detect_legacy_file()
            return self._reload_not_published(
                f"the scheduled triggers from {path} could not be published ({publish_fault}), "
                "so the current jobs are kept and automatic runs are paused. A reload is "
                "required (/jobs reload) once the scheduler driver is available."
            )
        if not published:
            self._legacy_notice = self._detect_legacy_file()
            return self._reload_not_published(
                f"{path} was being reloaded when it changed, so nothing was "
                "loaded and the current jobs are kept. Reload again (/jobs reload)."
            )
        logger.info("Scheduler reloaded: %d active, %d failed", reloaded, failed)
        return {"reloaded": reloaded, "failed": failed}

    @staticmethod
    def _reload_not_published(reason: str) -> dict:
        """Log and return a reload failure that published nothing and set no notice."""
        logger.warning("Scheduler reload not published: %s", reason)
        error = reason[0].upper() + reason[1:]
        return {"reloaded": 0, "failed": 0, "error": error}

    def _prepare_reload_registrations(self, candidate: dict) -> tuple[list, int, int]:
        """Prepare every enabled job's trigger on the detached *candidate* view.

        Returns ``(registrations, reloaded, failed)``. A trigger-driver failure
        is contained per job: that job gets a safe ``config_error``, loses its
        next-run fields, and has no registration, while siblings still prepare.
        Touches no global or published state and takes no lock.
        """
        registrations: list = []
        reloaded = failed = 0
        for tag, meta in candidate.items():
            if meta.get("config_error"):
                failed += 1
                continue
            if not meta.get("enabled", True):
                continue
            try:
                registrations.extend(self._prepare_registration(tag, meta))
            except _MutationFailed as exc:
                meta.pop("_next_run", None)
                meta.pop("_natural_next_run", None)
                meta["config_error"] = self._registration_error(exc.__cause__ or exc)
                logger.warning("Job '%s' is not scheduled: %s", tag, meta["config_error"])
                failed += 1
                continue
            reloaded += 1
        return registrations, reloaded, failed

    def _save_scheduler_toml(self, patch: Optional[Callable[[dict], bool]] = None) -> bool:
        """Persist an explicit document-only patch through the atomic mutation commit.

        *patch* edits a deep copy of the current authoritative document in
        place and returns False when there is nothing to persist; without a
        patch the current document is re-emitted. No job view or registration
        changes. Returns True when nothing needed writing or the write
        succeeded, False (logged) on any refusal or failure.
        """
        path = self._scheduler_config_path
        if self.reload_required_notice() is not None:
            logger.warning("Not writing %s: %s", path, self._reload_required_reason())
            return False
        if self._file_refusal() is not None:
            # Never overwrite a file that failed to load; the operator repairs it.
            logger.warning("Not writing %s: it failed to load. Repair it, then reload.", path)
            return False

        def build(_base: Optional[dict]) -> _MutationPlan:
            return _MutationPlan(meta=None, patch=patch or (lambda _document: True))

        failure = self._commit_mutation(None, build)
        if isinstance(failure, _ReloadRequiredReason):
            logger.warning("Scheduler file update needs a reload: %s", failure)
            return False
        if failure is not None:
            logger.warning("Not writing %s: %s", path, failure)
            return False
        return True

    # Bound on re-preparing a candidate made stale by concurrent publications.
    _MAX_MUTATION_ATTEMPTS = 5

    def _loop_running(self) -> bool:
        """Return True while the loop is active without a requested stop."""
        return bool(
            self.enabled
            and not self._stop_event.is_set()
            and self._thread is not None
            and self._thread.is_alive()
        )

    def _not_changed_error(self, tag: str, reason: str) -> str:
        """Return the public failure for a mutation that changed nothing."""
        return f"Job '{tag}' was not changed: {reason}"

    def _mutation_error(self, tag: str, failure: str) -> str:
        """Return the public failure; a reload-required reason is reported unwrapped."""
        if isinstance(failure, _ReloadRequiredReason):
            return str(failure)
        return self._not_changed_error(tag, failure)

    def _require_job(self, tag: str, base: Optional[dict]) -> dict:
        """Return *base*, or fail the mutation when the job no longer exists."""
        if base is None:
            raise _MutationFailed(
                f"job '{tag}' no longer exists, so {self._scheduler_config_path} "
                "was not written. List the jobs and retry."
            )
        return base

    def _removal_builder(self, tag: str) -> Callable[[Optional[dict]], _MutationPlan]:
        """Return the plan builder removing one job, its table, and its trigger."""
        def build(base: Optional[dict]) -> _MutationPlan:
            self._require_job(tag, base)
            return _MutationPlan(
                meta=None, patch=self._remove_table_patch(tag), registration="clear",
            )

        return build

    def _enabled_builder(
        self, tag: str, enabled: bool,
    ) -> Callable[[Optional[dict]], _MutationPlan]:
        """Return the plan builder pausing (clear trigger) or resuming (register) one job."""
        def build(base: Optional[dict]) -> _MutationPlan:
            meta = self._require_job(tag, base)
            meta["enabled"] = enabled
            return _MutationPlan(
                meta=meta,
                patch=self._job_fields_patch(tag, {"enabled": enabled}),
                registration="register" if enabled else "clear",
            )

        return build

    def _commit_mutation(
        self, tag: Optional[str], build: Callable[[Optional[dict]], _MutationPlan],
    ) -> Optional[str]:
        """Prepare, verify freshness, write, and publish one mutation as a unit.

        The candidate is prepared outside the commit locks. Under
        ``_running_lock`` then ``_save_lock`` the configuration generation is
        revalidated (a stale candidate is discarded and re-prepared from
        current state; *build* never runs the caller's attempt hook), a
        removal's legacy preflight fingerprint must still match the legacy
        file, the disk must still hold the stored bytes (or still be missing), the full
        replacement registration list is built, the bytes are
        written, and document, hash, job view, registrations, and generation
        are published together. Nothing that takes a lock, parses a schedule,
        or constructs a trigger runs after the write.

        Returns None on success, else an operator-safe failure reason; on
        failure the file, the stored document/hash, the job view, and the
        registrations are unchanged. Exceptions: while the RELOAD_REQUIRED
        latch is set (rechecked under the locks) nothing is attempted, and an
        ordinary exception while publishing after the write is contained: the
        latch is set, automatic triggers are quiesced, nothing is rolled back
        or retried, and a ``_ReloadRequiredReason`` is returned.
        """
        for _ in range(self._MAX_MUTATION_ATTEMPTS):
            try:
                prepared = self._prepare_mutation(tag, build)
            except _MutationFailed as exc:
                return exc.reason
            with self._running_lock, self._save_lock:
                if self._reload_required:
                    return self._reload_required_reason()
                if self._config_generation != prepared.generation:
                    logger.debug("Scheduler mutation candidate is stale; re-preparing.")
                    continue
                try:
                    replacement = self._replacement_registrations_locked(tag, prepared)
                except Exception as exc:  # noqa: BLE001 - external schedule jobs; contained before any write
                    # External job attributes can carry raw input; never include them.
                    logger.warning(
                        "Job '%s' registrations could not be prepared (%s); nothing was changed.",
                        tag, type(exc).__name__,
                    )
                    return (
                        f"the scheduled triggers could not be prepared ({type(exc).__name__}), "
                        f"so {self._scheduler_config_path} was not written. Check the scheduler "
                        "driver availability and the logs, then retry the change."
                    )
                if prepared.data is not None:
                    if not self._disk_unchanged_locked():
                        return self._external_change_reason()
                    if not self._write_scheduler_file_locked(prepared.data):
                        return (
                            f"{self._scheduler_config_path} could not be written, so the "
                            "previous file and schedule are kept. Check the file's "
                            "permissions, free space, and the logs, then retry the change."
                        )
                try:
                    self._publish_mutation_locked(tag, prepared, replacement)
                except Exception as exc:  # noqa: BLE001 - external job list after the write; contained under the latch
                    self._quiesce_registrations_locked()
                    # External exception messages can carry raw input; class only.
                    logger.warning(
                        "Publishing the change to %s failed after the write (%s); "
                        "a reload is required.", self._scheduler_config_path, type(exc).__name__,
                    )
                    return self._reload_required_reason()
                except BaseException:
                    self._quiesce_registrations_locked()
                    raise
            return None
        return (
            f"{self._scheduler_config_path} kept changing while the change was "
            "being prepared, so it was not written. Retry the change."
        )

    def _prepare_mutation(
        self, tag: Optional[str], build: Callable[[Optional[dict]], _MutationPlan],
    ) -> _PreparedMutation:
        """Build an unpublished candidate from a snapshot; takes only a brief snapshot lock.

        Snapshots the generation, stored load result, and a detached copy of
        the job view under ``_running_lock``, then (outside every lock)
        patches a deep copy of the document, proves the encoded text parses
        back unchanged, and prepares the affected trigger on the detached
        view. Raises ``_MutationFailed`` when the change cannot be prepared.
        """
        path = self._scheduler_config_path
        with self._running_lock:
            generation = self._config_generation
            stored = self._load_result
            live = self._jobs_meta.get(tag) if tag is not None else None
            base = copy.deepcopy(live) if live is not None else None
        plan = build(copy.deepcopy(base))

        data: Optional[bytes] = None
        load_result: Optional[SchedulerLoadResult] = None
        candidate = copy.deepcopy(stored.document) if stored.document is not None else {}
        try:
            if plan.patch(candidate):
                text = _SCHEDULER_FILE_HEADER + _encode_toml_document(candidate)
                written = tomli.loads(text)
                if not _toml_values_equal(written, candidate):
                    raise ValueError("the encoded document does not parse back unchanged")
                data = text.encode("utf-8")
                load_result = SchedulerLoadResult(
                    "valid", path, document=written,
                    content_hash=hashlib.sha256(data).hexdigest(),
                )
        except (ValueError, tomli.TOMLDecodeError) as exc:
            logger.warning("Not writing %s: %s.", path, exc)
            raise _MutationFailed(
                f"the change cannot be saved to {path} ({exc}); nothing was written."
            ) from exc

        new_meta = plan.meta if tag is not None else None
        registrations: tuple = ()
        replace = False
        if new_meta is not None and plan.registration == "register":
            target = new_meta if plan.publish_registration else copy.deepcopy(new_meta)
            prepared_jobs = self._prepare_registration(tag, target)
            if plan.publish_registration:
                registrations, replace = prepared_jobs, True
        elif plan.registration == "clear":
            replace = True

        meta_set: dict = {}
        meta_drop: frozenset = frozenset()
        if base is not None and new_meta is not None:
            # Type-strict, NaN-aware: True -> 1 and 1 -> 1.0 are changes.
            meta_set = {
                k: v for k, v in new_meta.items()
                if k not in base or not _toml_values_equal(base[k], v)
            }
            meta_drop = frozenset(base.keys() - new_meta.keys())
        return _PreparedMutation(
            generation=generation, data=data, load_result=load_result,
            new_meta=new_meta, meta_set=meta_set, meta_drop=meta_drop,
            remove=tag is not None and base is not None and new_meta is None,
            replace_registration=tag is not None and replace,
            registrations=registrations,
        )

    def _prepare_registration(self, tag: str, meta: dict) -> tuple:
        """Prepare *meta*'s trigger on the detached view without publishing it.

        Mirrors ``_register_job``: a job with a ``config_error`` stays
        unscheduled; a once job is built on an isolated ``schedule.Scheduler``
        and rebound to the default scheduler for later publication; a cron job
        gets its next-run fields set on *meta*. Returns the detached
        ``schedule`` jobs. Any trigger-driver failure raises ``_MutationFailed``
        with nothing global or published touched.
        """
        if meta.get("config_error"):
            meta.pop("_next_run", None)
            meta.pop("_natural_next_run", None)
            return ()
        try:
            if meta.get("schedule_type", "cron") == "once":
                isolated = schedule.Scheduler()
                run_at = meta.get("run_at") or meta.get("time")
                trigger = isolated.every().day.at(run_at) if run_at else isolated.every(1).minutes
                trigger.do(self._run_job, tag=tag).tag(tag)
                for job in isolated.jobs:
                    job.scheduler = schedule.default_scheduler
                return tuple(isolated.jobs)
            expr = meta.get("cron")
            if not expr:
                logger.warning("Job '%s' has no cron expression — skipping", tag)
                return ()
            natural_next = croniter(expr, datetime.now()).get_next(datetime)
            # Same natural-tick base and first-run jitter as _register_job.
            jitter_secs = random.randint(-self._JITTER_MAX_SECS, self._JITTER_MAX_SECS)
            meta["_natural_next_run"] = natural_next.isoformat()
            meta["_next_run"] = (natural_next + timedelta(seconds=jitter_secs)).isoformat()
            return ()
        except Exception as exc:  # noqa: BLE001 - external trigger drivers; contained before any write
            # Driver exception messages can contain raw input; never include them.
            logger.warning(
                "Job '%s' trigger could not be prepared (%s); nothing was changed.",
                tag, type(exc).__name__,
            )
            raise _MutationFailed(
                f"its schedule trigger could not be prepared ({type(exc).__name__}), so "
                f"{self._scheduler_config_path} was not written. Check the job schedule "
                "and scheduler driver availability, then retry the change."
            ) from exc

    def _disk_unchanged_locked(self) -> bool:
        """Return True when the file still holds the stored bytes, or is still missing.

        Called under both commit locks; takes no lock. The file must be
        readable and parseable and hash to the stored hash; a file recorded as
        missing must still be missing.
        """
        stored = self._load_result
        on_disk = _read_scheduler_file(self._scheduler_config_path)
        if stored.status == "missing":
            return on_disk.status == "missing"
        return on_disk.status == "valid" and on_disk.content_hash == stored.content_hash

    def _external_change_reason(self) -> str:
        """Return the failure reason for a file changed outside the scheduler."""
        path = self._scheduler_config_path
        logger.warning(
            "Not writing %s: it was changed outside the scheduler since it was last loaded.",
            path,
        )
        return (
            f"{path} was edited, created, removed, or damaged outside the scheduler "
            "since it was last loaded, so it was not overwritten. Review the file, "
            "then reload the scheduler (/jobs reload) and retry the change."
        )

    @staticmethod
    def _replacement_registrations_locked(
        tag: Optional[str], prepared: _PreparedMutation,
    ) -> Optional[list]:
        """Build the default scheduler's full replacement job list before the write.

        Called under both commit locks, before the file is written. Keeps
        every other tag's ``schedule`` job (same objects, same order) and
        appends the prepared registrations; returns None when the tag's
        registrations are not replaced. External job attributes may raise;
        the caller contains that before anything is written.
        """
        if tag is None or not prepared.replace_registration:
            return None
        return [job for job in schedule.default_scheduler.jobs if tag not in job.tags] + list(
            prepared.registrations
        )

    def _publish_mutation_locked(
        self, tag: Optional[str], prepared: _PreparedMutation, replacement: Optional[list],
    ) -> None:
        """Publish a committed candidate as one unit; caller holds both commit locks.

        Applies only the affected job's delta to its live view (keeping the
        view's identity and any concurrent runtime/history fields), installs
        the prebuilt *replacement* contents into the default scheduler's
        existing job list (keeping the list and every unrelated job object),
        and bumps the generation. Takes no lock and constructs or parses nothing.
        """
        if prepared.load_result is not None:
            self._load_result = prepared.load_result
        if tag is not None:
            live = self._jobs_meta.get(tag)
            if prepared.remove:
                self._jobs_meta.pop(tag, None)
            elif live is None:
                if prepared.new_meta is not None:
                    self._jobs_meta[tag] = prepared.new_meta
            else:
                live.update(prepared.meta_set)
                for key in prepared.meta_drop:
                    live.pop(key, None)
            if replacement is not None:
                schedule.default_scheduler.jobs[:] = replacement
        self._config_generation += 1

    def _write_scheduler_file_locked(self, data: bytes) -> bool:
        """Write *data* atomically with a backup; always called under both commit locks.

        Returns False when the write failed; the file then keeps its previous
        bytes and the caller publishes nothing. The successful ``os.replace``
        is the last fallible step: nothing that can fail runs after it. Takes
        no lock.
        """
        path = self._scheduler_config_path
        tmp = path + ".tmp"
        try:
            with open(tmp, "wb") as f:
                f.write(data)
            # Backup existing file before overwriting
            if os.path.exists(path):
                ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                shutil.copy2(path, path + f".bak.{ts}")
                self._prune_backups()
            logger.debug("Saving scheduler document to %s", path)
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("Could not save %s (%s); the previous file is kept.",
                           path, type(exc).__name__)
            try:
                os.remove(tmp)
            except OSError as cleanup_exc:
                logger.debug("Could not remove %s: %s", tmp, cleanup_exc)
            return False
        return True

    @staticmethod
    def _new_job_table(meta: dict) -> dict:
        """Return the raw TOML table persisted for a newly added job.

        Absent or ``None`` optional values are omitted (TOML has no null).
        """
        table: dict = {"enabled": meta.get("enabled", True)}
        if meta.get("schedule_type") == "once":
            table["schedule"] = "once"
            if meta.get("run_at") is not None:
                table["run_at"] = meta["run_at"]
        else:
            table["schedule"] = "cron"
            if meta.get("cron") is not None:
                table["cron"] = meta["cron"]
        table["task"] = meta.get("task", "")
        table["notify"] = meta.get("notify", True)
        if meta.get("model") is not None:
            table["model"] = meta["model"]
        if meta.get("preserve_context"):
            table["preserve_context"] = True
            ctx_max = meta.get("context_max_messages", 50)
            if type(ctx_max) is int and ctx_max != 50:
                table["context_max_messages"] = ctx_max
        if meta.get("overlap_policy", "skip") != "skip":
            table["overlap_policy"] = meta["overlap_policy"]
        source = meta.get("source", "config")
        if source != "config":
            table["source"] = source
        if meta.get("created_at"):
            table["created_at"] = meta["created_at"]
        return table

    def _job_fields_patch(
        self, tag: str, fields: dict, dropped: frozenset | set = frozenset(),
    ) -> Callable[[dict], bool]:
        """Return a patch setting *fields* and removing *dropped* keys of one job table.

        A job without a table in scheduler.toml (a legacy JSON job) is not
        written into it: the change stays in the runtime view only.
        """
        def patch(document: dict) -> bool:
            table = _job_tables(document).get(tag)
            if table is None:
                self._warn_not_in_file(tag)
                return False
            if type(table) is not dict:
                raise ValueError(f"job '{tag}' in the scheduler file is not a table")
            table.update(fields)
            for key in dropped:
                table.pop(key, None)
            return True

        return patch

    def _remove_table_patch(self, tag: str) -> Callable[[dict], bool]:
        """Return a patch removing one job's table; absent tables write nothing."""
        def patch(document: dict) -> bool:
            jobs = _job_tables(document)
            if tag not in jobs:
                self._warn_not_in_file(tag)
                return False
            del jobs[tag]
            return True

        return patch

    def _warn_not_in_file(self, tag: str) -> None:
        """Log that a change to a job outside scheduler.toml is runtime-only."""
        logger.warning(
            "Job '%s' is not in %s (legacy job file); the change applies until "
            "restart and is not saved.",
            tag, self._scheduler_config_path,
        )

    def _unsavable_error(self, tag: str, exc: ValueError) -> str:
        """Return the refusal for a value the scheduler file cannot represent."""
        return (
            f"Job '{tag}' was not changed: a value cannot be saved to "
            f"{self._scheduler_config_path} ({exc}). Use strings, numbers, "
            "booleans, dates, lists, or tables without empty (null) values."
        )

    def _read_config_jobs(self, config_path: str) -> tuple[SchedulerLoadResult, dict, int]:
        """Read scheduler.toml into a load result and a fresh job-meta map.

        Returns ``(load_result, jobs_meta, migrated_count)`` without touching
        state. Only a ``valid`` file yields jobs. The parsed document in the
        load result is never modified; each job's runtime view keeps its raw
        values, and a job whose scheduling fields are invalid carries a
        ``config_error`` and stays unscheduled. Legacy daily/interval fields
        derive a cron in memory. Callers publish the result into
        ``self._jobs_meta`` under ``_running_lock`` so readers never observe a
        partially built map.
        """
        new_meta: dict = {}
        load_result = _read_scheduler_file(config_path)
        if load_result.status == "missing":
            logger.info("No scheduler file at %s — no jobs loaded.", config_path)
            return load_result, new_meta, 0
        if load_result.document is None:
            return load_result, new_meta, 0

        jobs_section = load_result.document.get("jobs", {})
        if not isinstance(jobs_section, dict):
            logger.warning("Ignoring 'jobs' in %s: it must be a table of jobs.", config_path)
            jobs_section = {}
        migrated = 0
        for tag, job_cfg in jobs_section.items():
            new_meta[tag] = self._build_loaded_job(tag, job_cfg, config_path)
            if new_meta[tag].pop("_migrated", False):
                migrated += 1
                logger.info(
                    "Migrated job '%s' from schedule=%s to cron='%s'",
                    tag, job_cfg.get("schedule"), new_meta[tag]["cron"],
                )

        logger.info(
            "Loaded %d jobs from %s%s",
            len(new_meta), config_path,
            f" ({migrated} migrated to cron)" if migrated else "",
        )
        return load_result, new_meta, migrated

    @staticmethod
    def _build_loaded_job(
        tag: str, job_cfg: object, config_path: str, *, reject_obsolete: bool = True,
    ) -> dict:
        """Build one job's runtime view from its raw TOML table, type-strictly.

        Raw values are kept as parsed (no ``int()``/``bool()`` coercion and no
        model classification). A bad scheduling field, a non-boolean
        ``enabled``, or a non-table entry sets ``config_error``.
        """
        if not isinstance(job_cfg, dict):
            config_error: Optional[str] = "job entry must be a table"
            job_cfg = {}
            stype, cron, run_at, migrated = "cron", None, None, False
        else:
            stype, cron, run_at, config_error, migrated = _classify_schedule(
                job_cfg, reject_obsolete=reject_obsolete,
            )
        enabled = job_cfg.get("enabled", True)
        if config_error is None and not isinstance(enabled, bool):
            config_error = "enabled must be true or false"
        meta = {
            "tag": tag,
            "task": job_cfg.get("task", ""),
            "schedule_type": stype,
            "cron": cron,
            "run_at": run_at,
            "notify": job_cfg.get("notify", True),
            "enabled": enabled,
            "source": job_cfg.get("source", "config"),
            "last_run": None,
            "created_at": job_cfg.get("created_at", datetime.now(timezone.utc).isoformat()),
            "model": job_cfg.get("model"),
            "preserve_context": job_cfg.get("preserve_context", False),
            "context_max_messages": job_cfg.get("context_max_messages", 50),
            "overlap_policy": job_cfg.get("overlap_policy", "skip"),
            # Derived origin flag, never read from raw values: only the legacy
            # JSON reader marks its views read-only.
            "_legacy_read_only": False,
        }
        # Optional per-job step cap (raw; unused at runtime)
        if job_cfg.get("max_iterations") is not None:
            meta["max_iterations"] = job_cfg["max_iterations"]
        # A per-job fallback_models key (presence, any value) keeps the job scheduled; it is a
        # visible config error and fails the job's launch when it fires. No value is read.
        if reject_obsolete and "fallback_models" in job_cfg:
            meta["launch_config_error"] = _FALLBACK_MODELS_ERROR
            logger.warning("Job '%s' in %s has obsolete fallback_models: its launches will fail until removed.", tag, config_path)
        if config_error is not None:
            meta["config_error"] = config_error
            logger.warning(
                "Job '%s' in %s is not scheduled: %s. Fix the job, then reload.",
                tag, config_path, config_error,
            )
        elif migrated:
            meta["_migrated"] = True
        return meta

    def _load_config_jobs(self, config_path: str, sched_cfg: Optional[dict] = None) -> None:
        """Merge jobs from scheduler.toml into ``self._jobs_meta`` (startup path)."""
        # Legacy daily/interval crons are derived in memory only; a load never writes.
        load_result, new_meta, _ = self._read_config_jobs(config_path)
        with self._running_lock:
            self._load_result = load_result
            self._jobs_meta.update(new_meta)

    def _cron_trigger_snapshot(self, meta: dict) -> tuple:
        """Return one job's loop trigger inputs, read under ``_running_lock``.

        ``(enabled, schedule_type, cron, _next_run, _natural_next_run)``: the
        loop's due check reads them, and its cron advance is published only
        while they are unchanged.
        """
        with self._running_lock:
            return (
                meta.get("enabled", True),
                meta.get("schedule_type"),
                meta.get("cron"),
                meta.get("_next_run"),
                meta.get("_natural_next_run"),
            )

    def _run_loop(self) -> None:
        """Poll every 30 seconds. Fire cron jobs whose next_run has passed; run once-jobs via schedule lib."""
        while not self._stop_event.is_set():
            try:
                self._process_pending_commands()
                # Cron job check (local time)
                now_local = datetime.now()
                with self._running_lock:
                    jobs = list(self._jobs_meta.items())
                for tag, meta in jobs:
                    trigger = self._cron_trigger_snapshot(meta)
                    if not trigger[0] or trigger[1] not in ("cron", "daily", "interval", None):
                        continue
                    next_run_str = trigger[3]
                    if not next_run_str:
                        continue
                    try:
                        next_run = datetime.fromisoformat(next_run_str)
                    except ValueError:
                        continue
                    if now_local >= next_run:
                        # Fire in background thread
                        threading.Thread(target=self._run_job, kwargs={"tag": tag}, daemon=True).start()
                        # Schedule next occurrence using the natural (un-jittered) fire time as
                        # the croniter base. Using now_local would return the same tick when the
                        # job fired early due to negative startup jitter (double-execution bug).
                        # Computed outside every lock; published only while the job's
                        # trigger inputs still match the snapshot, so a cron committed
                        # meanwhile keeps its own next-run fields.
                        try:
                            expr = trigger[2]
                            if expr:
                                natural_str = trigger[4] or next_run_str
                                base_dt = datetime.fromisoformat(natural_str)
                                cron_iter = croniter(expr, base_dt)
                                next_iso = cron_iter.get_next(datetime).isoformat()
                                with self._running_lock:
                                    if self._jobs_meta.get(tag) is meta and _toml_values_equal(
                                        list(self._cron_trigger_snapshot(meta)), list(trigger),
                                    ):
                                        meta["_natural_next_run"] = next_iso
                                        meta["_next_run"] = next_iso
                        except (CroniterBadCronError, TypeError, ValueError) as exc:
                            logger.warning("Could not compute next_run for '%s': %s", tag, exc)
                # Once-jobs handled by schedule library
                # NOTE: Not wrapped in _running_lock. run_pending() synchronously
                # runs _run_job, so holding _running_lock across it would block
                # every reader (list_jobs, add_job, _save_scheduler_toml) for the
                # entire job duration. (_save_scheduler_toml snapshots under
                # _running_lock *before* taking _save_lock, so the lock order is
                # consistent and there is no AB-BA hazard either way.)
                schedule.run_pending()
                if self._warn_minutes > 0:
                    self._check_long_running_agents()
            except Exception:
                logger.exception("Scheduler _run_loop: unhandled exception in loop iteration — loop will continue")
            self._stop_event.wait(timeout=30)

    def _check_long_running_agents(self) -> None:
        """Warn once in chat when a sub-agent has been running longer than _warn_minutes."""
        try:
            from sub_agent_registry import get_registry as _get_reg
        except ImportError:
            return

        registry = _get_reg()
        threshold = self._warn_minutes * 60
        active_ids: set = set()

        for record in registry.list_active():
            active_ids.add(record.agent_id)
            if record.elapsed_seconds < threshold:
                continue
            if record.agent_id in self._warned_agent_ids:
                continue
            self._warned_agent_ids.add(record.agent_id)
            elapsed = record.elapsed_str()
            msg = (
                f"⏱ <b>Sub-agent running for {elapsed}</b>\n"
                f"Job: <code>{_html.escape(record.label)}</code> | "
                f"Model: <code>{_html.escape(record.model)}</code>\n"
                f"Task: {_html.escape(record.task_preview)}…\n"
                f"Agent ID: <code>{record.agent_id}</code> — use /agents to monitor or cancel"
            )
            logger.warning(
                "Long-running sub-agent detected: id=%s label=%s elapsed=%s",
                record.agent_id, record.label, elapsed,
            )
            try:
                self.notify(msg)
            except Exception as exc:
                logger.warning("_check_long_running_agents: notify failed: %s", exc)

        # Clean up ids for agents that have since finished
        self._warned_agent_ids &= active_ids

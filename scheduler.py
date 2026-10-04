"""
scheduler.py
------------
Background task scheduler backed by scheduler.toml and dynamic JSON storage.
Runs in a daemon thread so it does not block the Telegram bot.

Job sources:
  - scheduler.toml         static, config-managed jobs
  - scheduler_jobs.json    dynamic, runtime-managed jobs (XDG state home)

Scheduling uses cron expressions (5-field: minute hour day month weekday).
Old-style config fields (schedule=daily/interval, time=, hours=, minutes=) are
automatically migrated to equivalent cron expressions on load.

Examples:
  cron = "0 2 * * *"     → daily at 02:00
  cron = "0 */6 * * *"   → every 6 hours (00:00, 06:00, 12:00, 18:00)
  cron = "*/30 * * * *"  → every 30 minutes
"""

from __future__ import annotations

import html as _html
import json
import logging
import os
import random
import re
import shutil
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable, Optional

import schedule
from croniter import croniter, CroniterBadCronError

from sub_agent_registry import SOURCE_SCHEDULED
from sub_agent_supervisor import CANCELLED_SENTINEL, RunCompletion, SupervisionOptions
from telegram_formatter import ResultNotice, bound_display_text, format_result_notice

if TYPE_CHECKING:
    from xdg import XDGPaths

logger = logging.getLogger(__name__)

# Sentinel prefix used by legacy agent_fn to signal execution failure.
_ERROR_SENTINEL = "❌"


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


def _build_result_notice(
    tag: str, status: str, result: str, error_type: str, next_run: str, run_id: str,
) -> ResultNotice:
    """Build the run-result notice for a scheduled run's terminal outcome.

    The summary stays outside an expandable quote holding the result; a result
    too long for one message is replaced by the execution ID and a Details button.
    """
    summary = _result_summary(tag, status, error_type, next_run)
    if status == "cancelled":
        return ResultNotice(summary)
    return format_result_notice(summary, result, run_id)


def _result_summary(tag: str, status: str, error_type: str, next_run: str) -> str:
    """Return the Telegram-HTML outcome summary for a run, with the tag bounded."""
    safe_tag = _html.escape(bound_display_text(tag))
    if status == "failed":
        return (
            f"<b>⚠️ Scheduled job failed: {safe_tag}</b>\n"
            f"Error type: {_html.escape(error_type)}\n"
            f"Next run: {_html.escape(next_run)}"
        )
    if status == "cancelled":
        return f"<b>🛑 Scheduled job cancelled: {safe_tag}</b>"
    return f"<b>✅ Scheduled job finished: {safe_tag}</b>"


def _build_unretained_notice(
    tag: str, status: str, error_type: str, next_run: str,
) -> ResultNotice:
    """Build the notice for a run whose outcome could not be saved to job history.

    It carries the real outcome summary plus an honest note that the result was
    not retained; it has no Details control because no record exists.
    """
    summary = _result_summary(tag, status, error_type, next_run)
    return ResultNotice(
        f"{summary}\n\nThe result was not retained in job history. "
        "Details are unavailable."
    )


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
    ):
        sched_cfg = config.get("scheduler", {})
        agent_cfg = config.get("agent", {})
        self.enabled: bool = sched_cfg.get("enabled", True)
        self.notify = notify_fn
        self._result_notice_sink: Optional[Callable[[ResultNotice], None]] = None
        self._unretained_run_ids: set[str] = set()
        self.agent = agent_fn
        self.builtin_executor = builtin_executor  # Optional[BuiltinExecutor]
        self._scheduler_config_path = scheduler_config_path
        self._commands_file = str(paths.scheduler_commands)
        self._state_file = str(paths.scheduler_state)
        self._dynamic_jobs_file = str(paths.scheduler_jobs)

        # Execution history log
        _log_max_age = int(sched_cfg.get("execution_log_max_age_hours", 48))
        _log_max_per = int(sched_cfg.get("execution_log_max_per_job", 10))
        self.execution_log = JobExecutionLog(
            log_file=str(paths.job_execution_log),
            max_age_hours=_log_max_age,
            max_per_job=_log_max_per,
        )

        # Long-running agent watcher
        self._warn_minutes: int = int(agent_cfg.get("long_run_warn_minutes", 30))
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

        paths.state_home.mkdir(parents=True, exist_ok=True)
        self._load_config_jobs(scheduler_config_path, sched_cfg)
        self._load_dynamic_jobs()   # migration: imports old JSON, deletes it, writes to TOML
        self._load_state()          # overlay last_run/last_error onto active jobs from history
        self._save_state()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        if not self.enabled:
            logger.info("Scheduler is disabled in config.")
            return
        for tag, meta in self._jobs_meta.items():
            if meta.get("enabled", True):
                self._register_job(tag, meta)
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="scheduler")
        self._thread.start()
        logger.info("Scheduler started with %d jobs.", len(self._jobs_meta))

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        with self._running_lock:
            schedule.clear()
        logger.info("Scheduler stopped.")

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
        time_str: Optional[str],
        hours,
        minutes,
        run_at: Optional[str],
    ) -> tuple[Optional[str], str, Optional[str]]:
        """Resolve the cron expression and effective run_at for a new job.

        Returns ``(expr, schedule_type, effective_run_at)`` or raises
        ``ValueError`` with a user-facing message on invalid input.
        """
        if cron:
            expr = cron.strip()
            schedule_type = "cron"
        elif schedule_type == "once":
            expr = None  # once jobs don't use cron
        else:
            # Convert legacy style to cron
            expr = _legacy_to_cron(schedule_type, time_str, hours, minutes)
            if not expr:
                raise ValueError(
                    "Could not determine schedule. Provide a 'cron' expression "
                    "(e.g. '0 */6 * * *') or legacy fields."
                )
            schedule_type = "cron"

        # Validate cron expression
        if expr:
            try:
                croniter(expr)
            except (CroniterBadCronError, ValueError) as exc:
                raise ValueError(f"Invalid cron expression '{expr}': {exc}") from exc

        if schedule_type == "once" and not (run_at or time_str):
            raise ValueError("'run_at' (HH:MM) is required for once jobs")

        effective_run_at = run_at or time_str
        return expr, schedule_type, effective_run_at

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
        """Build the meta dict for a new job."""
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
        }
        if model:
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
        hours: Optional[int] = None,
        minutes: Optional[int] = None,
        time_str: Optional[str] = None,
        run_at: Optional[str] = None,
        cron: Optional[str] = None,
        source: str = "dynamic",
        model: Optional[str] = None,
        preserve_context: bool = False,
        context_max_messages: int = 50,
        overlap_policy: str = "skip",
    ) -> dict:
        # Normalize tag to underscore-separated lowercase (TOML-safe bare key)
        tag = _normalize_context_key(tag)
        # Coerce hours/minutes to int — LLM may pass them as strings
        try:
            hours = int(hours) if hours is not None else None
        except (ValueError, TypeError):
            hours = None
        try:
            minutes = int(minutes) if minutes is not None else None
        except (ValueError, TypeError):
            minutes = None

        if tag in self._jobs_meta:
            return {"success": False, "error": f"Job '{tag}' already exists."}

        # Resolve and validate schedule
        try:
            expr, schedule_type, effective_run_at = self._resolve_schedule(
                schedule_type, cron, time_str, hours, minutes, run_at,
            )
        except ValueError as exc:
            return {"success": False, "error": str(exc)}

        meta = self._build_job_meta(
            tag, task, schedule_type, expr, effective_run_at, notify, source,
            model, preserve_context, context_max_messages,
            overlap_policy,
        )

        with self._running_lock:
            self._jobs_meta[tag] = meta
        self._save_state()
        self._save_scheduler_toml()
        if self.enabled and self._thread and self._thread.is_alive():
            self.reload()
        logger.info("Job added: %s (%s)", tag, self._describe_schedule(meta))
        return {"success": True}

    def remove_job(self, tag: str) -> bool:
        tag = self._resolve_tag(tag) or tag
        with self._running_lock:
            if tag not in self._jobs_meta:
                return False
            schedule.clear(tag)
            del self._jobs_meta[tag]
        self._save_state()
        self._save_scheduler_toml()
        logger.info("Job removed: %s", tag)
        return True

    def pause_job(self, tag: str) -> bool:
        tag = self._resolve_tag(tag) or tag
        with self._running_lock:
            if tag not in self._jobs_meta:
                return False
            self._jobs_meta[tag]["enabled"] = False
            schedule.clear(tag)
        self._save_state()
        self._save_scheduler_toml()
        logger.info("Job paused: %s", tag)
        return True

    def resume_job(self, tag: str) -> bool:
        tag = self._resolve_tag(tag) or tag
        with self._running_lock:
            if tag not in self._jobs_meta:
                return False
            self._jobs_meta[tag]["enabled"] = True
            self._register_job(tag, self._jobs_meta[tag])
        self._save_state()
        self._save_scheduler_toml()
        logger.info("Job resumed: %s", tag)
        return True

    def run_now(self, tag: str) -> dict:
        """Trigger a job immediately in a background thread."""
        resolved = self._resolve_tag(tag)
        if not resolved:
            # Build a helpful error listing known tags
            known = ", ".join(self._jobs_meta.keys()) or "none"
            return {"success": False, "error": f"Job '{tag}' not found. Known jobs: {known}"}
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
        ``source``, ``enabled``, ``is_running``, and ``created_at``.
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
        return {
            "tag": canonical,
            "task": meta.get("task", ""),
            "schedule": self._describe_schedule(meta),
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

    def modify_job(self, tag: str, updates: dict) -> dict:
        """Partial-patch an existing job under the running lock.

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
        canonical = self._resolve_tag(tag) or tag
        with self._running_lock:
            if canonical not in self._jobs_meta:
                known = ", ".join(self._jobs_meta.keys()) or "none"
                return {"success": False, "error": f"Job '{tag}' not found. Known jobs: {known}"}

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
                        time_str=None,
                        hours=None,
                        minutes=None,
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

            # Re-register only when schedule fields changed AND the job is
            # enabled. pause_job clears the schedule-library registration; a
            # schedule patch on a paused job must not silently re-add it with a
            # fresh _next_run. resume_job remains the path back into the
            # registered state and re-registers with the already-patched meta.
            if schedule_changed and meta.get("enabled", True):
                self._register_job(canonical, meta)

        self._save_state()
        self._save_scheduler_toml()
        logger.info("Job modified: %s", canonical)
        return {"success": True}

    def list_jobs(self) -> list:
        with self._running_lock:
            running = set(self._running_jobs)
        result = []
        for tag, meta in self._jobs_meta.items():
            entry = {
                "tag": tag,
                "schedule": self._describe_schedule(meta),
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
            }
            result.append(entry)
        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

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

    def _register_job(self, tag: str, meta: dict) -> None:
        """Compute and store the next_run timestamp for cron jobs.
        Once jobs are registered with the schedule library (HH:MM trigger).
        """
        with self._running_lock:
            schedule.clear(tag)
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
            logger.warning("Could not compute next_run for job '%s' (%s): %s", tag, expr, exc)

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
        """Remove a completed once-job: clear from schedule, pop meta, persist state + TOML."""
        with self._running_lock:
            schedule.clear(tag)
            self._jobs_meta.pop(tag, None)
        self._save_state()
        self._save_scheduler_toml()

    def _run_job(self, tag: str, manual: bool = False) -> None:
        """Dispatch a scheduled job execution.

        Thin dispatcher: overlap check → route to the appropriate strategy.
        When ``manual`` is True the enabled check is skipped so run_now can
        fire paused jobs; scheduled firings continue to respect enabled.
        """
        meta = self._jobs_meta.get(tag)
        if not meta:
            return
        if not manual and not meta.get("enabled", True):
            return

        task = meta.get("task", "").strip()

        # Overlap detection and admission happen in one locked step, so a
        # finishing parallel sibling cannot mark the tag idle mid-admission.
        # Admitted agentic runs mint their independent execution ID here;
        # overlap skips and empty-task reminders never receive one.
        overlap_policy = meta.get("overlap_policy", "skip")
        run_id: Optional[str] = None
        with self._running_lock:
            if tag in self._running_jobs:
                if overlap_policy == "skip":
                    logger.warning(
                        "Job %s skipped — previous run still in progress (policy: skip)", tag
                    )
                    return
                # else: parallel — allow multiple instances
            if task:
                run_id = _new_run_id()
            self._running_jobs.add(tag)
            if run_id is not None:
                self._active_runs.setdefault(tag, set()).add(run_id)

        job_model = meta.get("model") or None
        _log_extra = f" | model={job_model}" if job_model else ""
        logger.info("Running scheduled job %s%s", tag, _log_extra)
        now = datetime.now(timezone.utc).isoformat()
        is_once = meta.get("schedule_type") == "once"

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
                notice = _build_unretained_notice(
                    completion.tag, status, error_type, next_run,
                )
            else:
                notice = _build_result_notice(
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
        if job_model:
            spawn_args["model"] = job_model
        if context_key:
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
                task=task, model=job_model or "", run_id=run_id,
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
            task=task, model=job_model or "", run_id=run_id,
        )

    def _fail_launch(
        self, tag: str, meta: dict, error_message: str, headline: str,
        *, task: str, model: str, run_id: str,
    ) -> None:
        """Record a launch failure, alert directly (independent of notify), release the run.

        The admitted run's failed execution record carries its run ID and the
        launch error, with failure classification and next-run metadata.
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
                    hours = int(cmd["hours"]) if "hours" in cmd else None
                    mins = int(cmd["minutes"]) if "minutes" in cmd else None
                    t = cmd.get("time")
                    notify = str(cmd.get("notify", "true")).lower() != "false"
                    res = self.add_job(
                        tag=tag,
                        schedule_type=stype,
                        task=cmd.get("task", ""),
                        notify=notify,
                        hours=hours,
                        minutes=mins,
                        time_str=t,
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

    def _save_state_locked(self, jobs_snapshot: list[tuple[str, dict]]) -> None:
        # Merge current job states into run_history so history is never lost
        for tag, meta in jobs_snapshot:
            if meta.get("last_run") or meta.get("last_error"):
                self._run_history[tag] = {
                    "last_run": meta.get("last_run"),
                    "last_error": meta.get("last_error"),
                }
        state = {
            "jobs": {
                tag: {
                    **meta,
                    "schedule_description": self._describe_schedule(meta),
                }
                for tag, meta in jobs_snapshot
            },
            "history": self._run_history,
        }
        tmp = self._state_file + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, self._state_file)
        except OSError as exc:
            logger.warning("Could not save scheduler state: %s", exc)

    def _load_state(self) -> None:
        """Load run history from state file and overlay last_run/last_error onto active jobs."""
        if not os.path.exists(self._state_file):
            return
        try:
            with open(self._state_file) as f:
                state = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not load scheduler state: %s", exc)
            return

        # Restore run history (all jobs ever run, including removed ones)
        self._run_history = state.get("history", {})

        # Fall back to reading history from old-format state (jobs section had last_run)
        if not self._run_history:
            for tag, data in state.get("jobs", {}).items():
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

    def _load_dynamic_jobs(self) -> None:
        """Migration: import old scheduled_jobs.json into TOML, then delete the JSON file."""
        if not os.path.exists(self._dynamic_jobs_file):
            return
        try:
            with open(self._dynamic_jobs_file) as f:
                jobs = json.load(f)
            imported = 0
            for tag, meta in jobs.items():
                if tag not in self._jobs_meta:
                    meta["source"] = "dynamic"
                    self._jobs_meta[tag] = meta
                    imported += 1
            if imported:
                logger.info("Migrated %d jobs from %s → scheduler.toml", imported, self._dynamic_jobs_file)
                self._save_scheduler_toml()
            os.remove(self._dynamic_jobs_file)
            logger.info("Removed legacy %s after migration", self._dynamic_jobs_file)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not migrate dynamic jobs: %s", exc)

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
        Hot-reload scheduler.toml: clear all registered jobs, re-read the file,
        and re-register all enabled jobs. Safe to call while the scheduler is running.
        Returns {"reloaded": N, "failed": N}.
        """
        logger.info("Reloading scheduler.toml…")
        # Parse and fully populate the replacement map BEFORE publishing it. A
        # reader that legitimately holds _running_lock (e.g. _save_scheduler_toml's
        # snapshot, fired by a once-job finishing) must never observe the empty or
        # half-filled map — it would persist an empty scheduler.toml and lose every
        # job, or trip "dictionary changed size during iteration".
        new_meta, migrated = self._read_config_jobs(self._scheduler_config_path)
        # Re-overlay run history so last_run/last_error survive a reload
        for tag, meta in new_meta.items():
            if tag in self._run_history:
                hist = self._run_history[tag]
                if hist.get("last_run"):
                    meta["last_run"] = hist["last_run"]
                if hist.get("last_error"):
                    meta["last_error"] = hist["last_error"]
        with self._running_lock:
            schedule.clear()
            self._jobs_meta = new_meta
        if migrated:
            # Write back migrated cron expressions
            self._save_scheduler_toml()
        reloaded = 0
        failed = 0
        for tag, meta in list(self._jobs_meta.items()):
            if meta.get("enabled", True):
                try:
                    self._register_job(tag, meta)
                    reloaded += 1
                except (CroniterBadCronError, TypeError, ValueError) as exc:
                    logger.warning("Failed to register job '%s' on reload: %s", tag, exc)
                    failed += 1
        logger.info("Scheduler reloaded: %d active, %d failed", reloaded, failed)
        return {"reloaded": reloaded, "failed": failed}

    def _save_scheduler_toml(self) -> None:
        """Persist all current jobs to scheduler.toml (auto-managed file)."""
        # Snapshot before _save_lock to avoid AB-BA deadlock with _run_job.
        with self._running_lock:
            jobs_snapshot = [(tag, dict(meta)) for tag, meta in self._jobs_meta.items()]
        with self._save_lock:
            self._save_scheduler_toml_locked(jobs_snapshot)

    @staticmethod
    def _serialize_job(tag: str, meta: dict) -> list[str]:
        """Serialize a single job's metadata to TOML lines.

        Hand-rolled serializer — no TOML write dependency. Handles escaping,
        inline arrays, and conditional fields.
        """
        def _toml_str(v: str) -> str:
            return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'

        lines: list[str] = []
        lines.append(f"[jobs.{tag}]\n")
        lines.append(f"enabled = {str(meta.get('enabled', True)).lower()}\n")
        stype = meta.get("schedule_type", "cron")
        if stype == "once":
            lines.append('schedule = "once"\n')
            if meta.get("run_at"):
                lines.append(f'run_at = "{meta["run_at"]}"\n')
        else:
            lines.append('schedule = "cron"\n')
            if meta.get("cron"):
                lines.append(f'cron = "{meta["cron"]}"\n')
        lines.append(f"task = {_toml_str(meta.get('task', ''))}\n")
        lines.append(f"notify = {str(meta.get('notify', True)).lower()}\n")
        if meta.get("model"):
            lines.append(f"model = {_toml_str(meta['model'])}\n")
        if meta.get("preserve_context"):
            lines.append('preserve_context = true\n')
            ctx_max = meta.get("context_max_messages", 50)
            if ctx_max != 50:
                lines.append(f'context_max_messages = {ctx_max}\n')
        if meta.get("overlap_policy", "skip") != "skip":
            lines.append(f'overlap_policy = "{meta["overlap_policy"]}"\n')
        if meta.get("max_iterations") is not None:
            lines.append(f'max_iterations = {int(meta["max_iterations"])}\n')
        source = meta.get("source", "config")
        if source != "config":
            lines.append(f'source = "{source}"\n')
        if meta.get("created_at"):
            lines.append(f'created_at = "{meta["created_at"]}"\n')
        lines.append("\n")
        return lines

    def _save_scheduler_toml_locked(self, jobs_snapshot: list[tuple[str, dict]]) -> None:
        """Inner (lock-free) implementation — always called under _save_lock."""
        lines = [
            "# scheduler.toml — auto-managed by scheduler\n",
            "# Edit this file to add static jobs; dynamic/user jobs are appended here.\n",
            "# Schedules use cron expressions (5-field, local server time):\n",
            "#   minute hour day month weekday\n",
            "#   Examples: '0 2 * * *' = daily at 02:00, '0 */6 * * *' = every 6h\n",
            "\n",
        ]
        for tag, meta in jobs_snapshot:
            lines.extend(self._serialize_job(tag, meta))

        tmp = self._scheduler_config_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(lines)
            # Backup existing file before overwriting
            if os.path.exists(self._scheduler_config_path):
                ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                bak = self._scheduler_config_path + f".bak.{ts}"
                shutil.copy2(self._scheduler_config_path, bak)
                self._prune_backups()
            os.replace(tmp, self._scheduler_config_path)
            logger.debug("Saved %d jobs to %s", len(self._jobs_meta), self._scheduler_config_path)
        except OSError as exc:
            logger.warning("Could not save scheduler.toml: %s", exc)

    def _read_config_jobs(self, config_path: str) -> tuple[dict, int]:
        """Parse scheduler.toml into a fresh job-meta map without touching state.

        Returns ``(jobs_meta, migrated_count)``. Migrates legacy daily/interval
        fields to cron. Callers publish the result into ``self._jobs_meta`` under
        ``_running_lock`` so readers never observe a partially built map.
        """
        new_meta: dict = {}
        if not os.path.exists(config_path):
            logger.warning(
                "scheduler.toml not found at %s — no jobs loaded. "
                "Create the file to configure scheduled jobs.",
                config_path,
            )
            return new_meta, 0
        try:
            with open(config_path, "rb") as f:
                toml_data = tomli.load(f)
        except (OSError, tomli.TOMLDecodeError) as exc:
            logger.warning("Could not load %s: %s — starting with no jobs", config_path, exc)
            return new_meta, 0

        jobs_section = toml_data.get("jobs", {})
        migrated = 0
        for tag, job_cfg in jobs_section.items():
            stype = job_cfg.get("schedule", "cron")
            cron_expr = job_cfg.get("cron")

            # Migrate legacy schedule types to cron
            if stype in ("daily", "interval") and not cron_expr:
                cron_expr = _legacy_to_cron(
                    stype,
                    time_str=job_cfg.get("time"),
                    hours=job_cfg.get("hours"),
                    minutes=job_cfg.get("minutes"),
                )
                if cron_expr:
                    migrated += 1
                    logger.info(
                        "Migrated job '%s' from schedule=%s to cron='%s'",
                        tag, stype, cron_expr,
                    )
                stype = "cron"

            new_meta[tag] = {
                "tag": tag,
                "task": job_cfg.get("task", ""),
                "schedule_type": stype,
                "cron": cron_expr,
                "run_at": job_cfg.get("run_at") or job_cfg.get("time") if stype == "once" else None,
                "notify": job_cfg.get("notify", True),
                "enabled": job_cfg.get("enabled", True),
                "source": job_cfg.get("source", "config"),
                "last_run": None,
                "created_at": job_cfg.get("created_at", datetime.now(timezone.utc).isoformat()),
                "model": job_cfg.get("model") or None,
                "preserve_context": bool(job_cfg.get("preserve_context", False)),
                "context_max_messages": int(job_cfg.get("context_max_messages", 50)),
                "overlap_policy": job_cfg.get("overlap_policy", "skip"),
            }
            # Deprecation: drop fallback_models from loaded jobs, warn if present
            if "fallback_models" in job_cfg:
                logger.warning(
                    "Job '%s' has a deprecated 'fallback_models' field in scheduler.toml — "
                    "ignored and dropped. The LLM client is now single-model.",
                    tag,
                )
            # Optional per-job step cap
            if job_cfg.get("max_iterations") is not None:
                try:
                    new_meta[tag]["max_iterations"] = int(job_cfg["max_iterations"])
                except (ValueError, TypeError):
                    pass

        logger.info(
            "Loaded %d jobs from %s%s",
            len(new_meta), config_path,
            f" ({migrated} migrated to cron)" if migrated else "",
        )
        return new_meta, migrated

    def _load_config_jobs(self, config_path: str, sched_cfg: Optional[dict] = None) -> None:
        """Merge jobs from scheduler.toml into ``self._jobs_meta`` (startup path)."""
        new_meta, migrated = self._read_config_jobs(config_path)
        with self._running_lock:
            self._jobs_meta.update(new_meta)
        if migrated:
            # Write back migrated cron expressions
            self._save_scheduler_toml()

    def _run_loop(self) -> None:
        """Poll every 30 seconds. Fire cron jobs whose next_run has passed; run once-jobs via schedule lib."""
        while not self._stop_event.is_set():
            try:
                self._process_pending_commands()
                # Cron job check (local time)
                now_local = datetime.now()
                for tag, meta in list(self._jobs_meta.items()):
                    if not meta.get("enabled", True):
                        continue
                    if meta.get("schedule_type") not in ("cron", "daily", "interval", None):
                        continue
                    next_run_str = meta.get("_next_run")
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
                        try:
                            expr = meta.get("cron")
                            if expr:
                                natural_str = meta.get("_natural_next_run") or next_run_str
                                base_dt = datetime.fromisoformat(natural_str)
                                cron_iter = croniter(expr, base_dt)
                                next_dt = cron_iter.get_next(datetime)
                                meta["_natural_next_run"] = next_dt.isoformat()
                                meta["_next_run"] = next_dt.isoformat()
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

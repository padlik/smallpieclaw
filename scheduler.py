"""
scheduler.py
------------
Background task scheduler backed by scheduler.toml and dynamic JSON storage.
Runs in a daemon thread so it does not block the Telegram bot.

Job sources:
  - scheduler.toml     static, config-managed jobs
  - data/scheduled_jobs.json  dynamic, runtime-managed jobs

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
import shutil
import threading
from datetime import datetime, timedelta
from typing import Callable, Optional

import schedule
from croniter import croniter, CroniterBadCronError

logger = logging.getLogger(__name__)

try:
    import tomli
except ImportError:
    import tomllib as tomli  # Python 3.11+


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _legacy_to_cron(stype: str, time_str: str = None, hours=None, minutes=None, run_at: str = None) -> Optional[str]:
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

_RESULT_MAX_CHARS = 2000  # truncate result stored per entry


class JobExecutionLog:
    """Compact execution log for scheduled jobs, stored as JSONL.

    Each entry:
        {"ts": ISO8601, "tag": str, "task": str, "result": str,
         "success": bool, "elapsed_s": int, "model": str}

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
    ) -> None:
        """Append a new entry and rotate old ones.  Thread-safe."""
        entry = {
            "ts": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tag": tag,
            "task": (task or "")[:200],
            "result": (result or "")[:_RESULT_MAX_CHARS],
            "success": bool(success),
            "elapsed_s": int(elapsed_s),
            "model": model or "",
        }
        with self._lock:
            entries = self._load()
            entries.append(entry)
            entries = self._rotate(entries)
            self._write(entries)
        logger.debug("JobExecutionLog: recorded entry for tag=%s success=%s", tag, success)

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
        """Return the most recent ``n`` entries (newest last)."""
        with self._lock:
            entries = self._load()
        return entries[-n:]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self) -> list[dict]:
        """Load all entries from the JSONL file.  Caller must hold self._lock."""
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
        return entries

    def _rotate(self, entries: list[dict]) -> list[dict]:
        """Remove stale entries.  Returns a new (filtered) list."""
        cutoff = datetime.utcnow() - timedelta(hours=self._max_age_hours)
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

    def _write(self, entries: list[dict]) -> None:
        """Atomically write entries back to the JSONL file.  Caller must hold self._lock."""
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
        agent_fn: Optional[Callable[[str], str]] = None,
        scheduler_config_path: str = "scheduler.toml",
        data_dir: str = "data",
        long_term_memory=None,
        builtin_executor=None,
    ):
        sched_cfg = config.get("scheduler", {})
        agent_cfg = config.get("agent", {})
        self.enabled: bool = sched_cfg.get("enabled", True)
        self.notify = notify_fn
        self.agent = agent_fn
        self.builtin_executor = builtin_executor  # Optional[BuiltinExecutor]
        self.long_term_memory = long_term_memory
        self._data_dir = data_dir
        self._scheduler_config_path = scheduler_config_path
        self._commands_file = os.path.join(data_dir, "scheduler_commands.json")
        self._state_file = os.path.join(data_dir, "scheduler_state.json")
        self._dynamic_jobs_file = os.path.join(data_dir, "scheduled_jobs.json")

        # Execution history log
        _log_max_age = int(sched_cfg.get("execution_log_max_age_hours", 48))
        _log_max_per = int(sched_cfg.get("execution_log_max_per_job", 10))
        self.execution_log = JobExecutionLog(
            log_file=os.path.join(data_dir, "job_execution_log.jsonl"),
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
        self._running_lock = threading.Lock()
        # Serializes all writes to scheduler.toml and scheduler_state.json,
        # preventing race conditions when two jobs finish simultaneously.
        self._save_lock = threading.Lock()

        os.makedirs(data_dir, exist_ok=True)
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
        normalized = tag.strip().lower()
        normalized = normalized.replace("-", "_").replace(" ", "_").replace(".", "_")
        while "__" in normalized:
            normalized = normalized.replace("__", "_")
        if normalized in self._jobs_meta:
            return normalized
        # Last resort: compare normalized versions of all stored keys
        for stored in self._jobs_meta:
            stored_norm = stored.lower().replace("-", "_").replace(" ", "_")
            if stored_norm == normalized:
                return stored
        return None

    def add_job(
        self,
        tag: str,
        schedule_type: str,
        task: str,
        notify: bool = True,
        hours=None,
        minutes=None,
        time_str: str = None,
        run_at: str = None,
        cron: str = None,
        source: str = "dynamic",
        model: str = None,
        fallback_models: list = None,
        preserve_context: bool = False,
        context_max_messages: int = 50,
        overlap_policy: str = "skip",
        max_iterations: int = None,
    ) -> dict:
        # Normalize tag to underscore-separated lowercase (TOML-safe bare key)
        tag = tag.strip().lower().replace("-", "_").replace(" ", "_").replace(".", "_")
        while "__" in tag:
            tag = tag.replace("__", "_")
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

        # Resolve cron expression
        if cron:
            expr = cron.strip()
            schedule_type = "cron"
        elif schedule_type == "once":
            expr = None  # once jobs don't use cron
        else:
            # Convert legacy style to cron
            expr = _legacy_to_cron(schedule_type, time_str, hours, minutes)
            if not expr:
                return {"success": False, "error": "Could not determine schedule. Provide a 'cron' expression (e.g. '0 */6 * * *') or legacy fields."}
            schedule_type = "cron"

        # Validate cron expression
        if expr:
            try:
                croniter(expr)
            except (CroniterBadCronError, ValueError) as exc:
                return {"success": False, "error": f"Invalid cron expression '{expr}': {exc}"}

        if schedule_type == "once" and not (run_at or time_str):
            return {"success": False, "error": "'run_at' (HH:MM) is required for once jobs"}

        effective_run_at = run_at or time_str
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
            "created_at": datetime.utcnow().isoformat(),
        }
        if model:
            meta["model"] = model
        if fallback_models is not None:
            meta["fallback_models"] = fallback_models
        if preserve_context:
            meta["preserve_context"] = preserve_context
            meta["context_max_messages"] = context_max_messages
        if overlap_policy != "skip":
            meta["overlap_policy"] = overlap_policy
        if max_iterations is not None:
            try:
                meta["max_iterations"] = int(max_iterations)
            except (ValueError, TypeError):
                pass

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
        import threading as _threading
        _threading.Thread(target=self._run_job, kwargs={"tag": resolved}, daemon=True).start()
        logger.info("Job '%s' triggered manually (run_now)", resolved)
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
                "fallback_models": meta.get("fallback_models"),
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

    def _mark_job_finished(self, tag: str) -> None:
        """Called by sub-agent thread when a scheduled job's sub-agent completes."""
        with self._running_lock:
            self._running_jobs.discard(tag)

    def _run_job(self, tag: str) -> None:
        meta = self._jobs_meta.get(tag)
        if not meta or not meta.get("enabled", True):
            return

        _spfx = f"[sched/{tag}] "

        # Overlap detection
        overlap_policy = meta.get("overlap_policy", "skip")
        with self._running_lock:
            if tag in self._running_jobs:
                if overlap_policy == "skip":
                    logger.warning(
                        "%sJob skipped — previous run still in progress (policy: skip)", _spfx
                    )
                    return
                # else: parallel — allow multiple instances
            self._running_jobs.add(tag)

        task = meta.get("task", "").strip()
        job_model = meta.get("model") or None
        job_fallbacks = meta.get("fallback_models")
        _log_extra = f" | model={job_model}" if job_model else ""
        if job_fallbacks is not None:
            _log_extra += f" | fallback_models={job_fallbacks}"
        logger.info("%sRunning scheduled job%s", _spfx, _log_extra)
        now = datetime.utcnow().isoformat()
        is_once = meta.get("schedule_type") == "once"

        # --- Empty task guard ---
        if not task:
            logger.warning("%sNo task — sending direct notification", _spfx)
            friendly = tag.replace("_", " ")
            meta["last_run"] = now
            self._run_history[tag] = {"last_run": now, "last_error": None}
            self._save_state()
            with self._running_lock:
                self._running_jobs.discard(tag)
            if is_once:
                schedule.clear(tag)
                self._jobs_meta.pop(tag, None)
                self._save_state()
                self._save_scheduler_toml()  # structural change: job removed
            if meta.get("notify", True):
                self.notify(f"🔔 <b>Reminder:</b> {_html.escape(friendly)}")
            return

        # Prefer spawn_agent via builtin_executor if available
        if self.builtin_executor is not None and hasattr(self.builtin_executor, '_exec_spawn_agent'):
            preserve_ctx = meta.get("preserve_context", False)
            context_key = tag if preserve_ctx else None

            spawn_args = {"task": task, "_job_tag": tag}
            if job_model:
                spawn_args["model"] = job_model
            if context_key:
                spawn_args["context_key"] = context_key
            # fallback_models: if key present in meta (even as []), pass it through;
            # if absent, omit so sub-agent inherits from parent config
            if "fallback_models" in meta:
                spawn_args["fallback_models"] = meta["fallback_models"]
            # max_iterations: per-job override; None = factory uses scheduled_max_iterations
            if "max_iterations" in meta:
                spawn_args["max_iterations"] = meta["max_iterations"]
            # Pass finish callback directly in spawn_args to avoid race when
            # multiple jobs fire concurrently and overwrite the shared attribute.
            spawn_args["_finish_cb"] = self._mark_job_finished
            # Scheduled job results should be shown as plain text, not in a
            # collapsed expandable blockquote (which hides the result by default).
            spawn_args["expandable"] = False
            # Honour the job's notify setting — False suppresses Telegram output.
            spawn_args["_notify"] = meta.get("notify", True)
            # Pass execution log callback so spawn_agent records the result.
            spawn_args["_result_log_cb"] = self.execution_log.record

            # Update last_run before spawning (we know it started)
            meta["last_run"] = now
            self._run_history.setdefault(tag, {})["last_run"] = now
            self._save_state()

            result = self.builtin_executor._exec_spawn_agent(spawn_args)
            if not result.get("success"):
                logger.error("%sSpawn failed: %s", _spfx, result.get("error"))
                meta["last_error"] = result.get("error", "spawn failed")
                self._run_history[tag]["last_error"] = meta["last_error"]
                self._save_state()
                # Always notify on error
                self.notify(
                    f"⚠️ <b>Job {_html.escape(tag)} failed to spawn</b>\n{_html.escape(str(meta['last_error']))}"
                )
                with self._running_lock:
                    self._running_jobs.discard(tag)
            else:
                # _running_jobs will be cleaned up by _mark_job_finished callback
                # Auto-remove once jobs (spawn_agent will still deliver result)
                if is_once:
                    schedule.clear(tag)
                    self._jobs_meta.pop(tag, None)
                    self._save_state()
                    self._save_scheduler_toml()
            return

        # Fallback: direct agent_fn call (legacy / no builtin_executor)
        result = ""
        error_occurred = False
        try:
            if self.agent:
                result = self.agent(task)
            else:
                result = "Agent not available for task."
            if result.startswith("❌"):
                error_occurred = True
        except (OSError, RuntimeError, ValueError) as exc:
            logger.error("%sFailed: %s", _spfx, exc)
            result = f"Job failed: {exc}"
            error_occurred = True
        finally:
            with self._running_lock:
                self._running_jobs.discard(tag)

        meta["last_run"] = now
        if error_occurred:
            meta["last_error"] = result
        else:
            meta.pop("last_error", None)

        self._run_history[tag] = {
            "last_run": meta["last_run"],
            "last_error": meta.get("last_error"),
        }
        self._save_state()

        # Record in execution log
        self.execution_log.record(
            tag=tag,
            task=task,
            result=result,
            success=not error_occurred,
            elapsed_s=0,
            model=job_model or "",
        )

        if error_occurred:
            if meta.get("notify", True):
                self.notify(f"⚠️ <b>Scheduled job failed:</b> <code>{_html.escape(tag)}</code>\n\n{_html.escape(result)}")
            return

        if tag == "longterm_memory_update" and self.long_term_memory:
            try:
                self.long_term_memory.add(result, source="scheduled")
                logger.info("%sLong-term memory updated", _spfx)
            except Exception as exc:
                logger.warning("%sFailed to update long-term memory: %s", _spfx, exc)

        # Auto-remove once/reminder jobs after successful execution
        if is_once:
            logger.info("%sOnce job completed — removing", _spfx)
            schedule.clear(tag)
            self._jobs_meta.pop(tag, None)
            self._save_state()
            self._save_scheduler_toml()  # structural change: job removed

        if meta.get("notify", True):
            self.notify(f"📅 <b>Scheduled: {_html.escape(tag)}</b>\n\n{_html.escape(result)}")

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
        with self._save_lock:
            self._save_state_locked()

    def _save_state_locked(self) -> None:
        # Merge current job states into run_history so history is never lost
        for tag, meta in self._jobs_meta.items():
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
                for tag, meta in self._jobs_meta.items()
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
        schedule.clear()
        self._jobs_meta = {}
        self._load_config_jobs(self._scheduler_config_path)
        # Re-overlay run history so last_run/last_error survive a reload
        for tag, meta in self._jobs_meta.items():
            if tag in self._run_history:
                hist = self._run_history[tag]
                if hist.get("last_run"):
                    meta["last_run"] = hist["last_run"]
                if hist.get("last_error"):
                    meta["last_error"] = hist["last_error"]
        reloaded = 0
        failed = 0
        for tag, meta in self._jobs_meta.items():
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
        with self._save_lock:
            self._save_scheduler_toml_locked()

    def _save_scheduler_toml_locked(self) -> None:
        """Inner (lock-free) implementation — always called under _save_lock."""
        def _toml_str(v: str) -> str:
            return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'

        lines = [
            "# scheduler.toml — auto-managed by scheduler\n",
            "# Edit this file to add static jobs; dynamic/user jobs are appended here.\n",
            "# Schedules use cron expressions (5-field, local server time):\n",
            "#   minute hour day month weekday\n",
            "#   Examples: '0 2 * * *' = daily at 02:00, '0 */6 * * *' = every 6h\n",
            "\n",
        ]
        for tag, meta in self._jobs_meta.items():
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
            if meta.get("fallback_models") is not None:
                fb = meta["fallback_models"]
                # Serialize as TOML inline array using the same escaping as _toml_str
                items = ", ".join(_toml_str(m) for m in fb)
                lines.append(f"fallback_models = [{items}]\n")
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

        tmp = self._scheduler_config_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(lines)
            # Backup existing file before overwriting
            if os.path.exists(self._scheduler_config_path):
                ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
                bak = self._scheduler_config_path + f".bak.{ts}"
                shutil.copy2(self._scheduler_config_path, bak)
                self._prune_backups()
            os.replace(tmp, self._scheduler_config_path)
            logger.debug("Saved %d jobs to %s", len(self._jobs_meta), self._scheduler_config_path)
        except OSError as exc:
            logger.warning("Could not save scheduler.toml: %s", exc)

    def _load_config_jobs(self, config_path: str, sched_cfg: dict = None) -> None:
        """Load all jobs from scheduler.toml. Migrates legacy daily/interval fields to cron."""
        if not os.path.exists(config_path):
            logger.warning(
                "scheduler.toml not found at %s — no jobs loaded. "
                "Create the file to configure scheduled jobs.",
                config_path,
            )
            return
        try:
            with open(config_path, "rb") as f:
                toml_data = tomli.load(f)
        except (OSError, tomli.TOMLDecodeError) as exc:
            logger.warning("Could not load %s: %s — starting with no jobs", config_path, exc)
            return

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

            self._jobs_meta[tag] = {
                "tag": tag,
                "task": job_cfg.get("task", ""),
                "schedule_type": stype,
                "cron": cron_expr,
                "run_at": job_cfg.get("run_at") or job_cfg.get("time") if stype == "once" else None,
                "notify": job_cfg.get("notify", True),
                "enabled": job_cfg.get("enabled", True),
                "source": job_cfg.get("source", "config"),
                "last_run": None,
                "created_at": job_cfg.get("created_at", datetime.utcnow().isoformat()),
                "model": job_cfg.get("model") or None,
                "fallback_models": job_cfg.get("fallback_models"),
                "preserve_context": bool(job_cfg.get("preserve_context", False)),
                "context_max_messages": int(job_cfg.get("context_max_messages", 50)),
                "overlap_policy": job_cfg.get("overlap_policy", "skip"),
            }
            # Optional per-job step cap
            if job_cfg.get("max_iterations") is not None:
                try:
                    self._jobs_meta[tag]["max_iterations"] = int(job_cfg["max_iterations"])
                except (ValueError, TypeError):
                    pass

        logger.info(
            "Loaded %d jobs from %s%s",
            len(self._jobs_meta), config_path,
            f" ({migrated} migrated to cron)" if migrated else "",
        )
        if migrated:
            # Write back migrated cron expressions
            self._save_scheduler_toml()

    def _run_loop(self) -> None:
        """Poll every 30 seconds. Fire cron jobs whose next_run has passed; run once-jobs via schedule lib."""
        while not self._stop_event.is_set():
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
            schedule.run_pending()
            if self._warn_minutes > 0:
                self._check_long_running_agents()
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


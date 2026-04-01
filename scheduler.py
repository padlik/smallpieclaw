"""
scheduler.py
------------
Background task scheduler backed by scheduler.toml and dynamic JSON storage.
Runs in a daemon thread so it does not block the Telegram bot.

Job sources:
  - scheduler.toml     static, config-managed jobs
  - data/scheduled_jobs.json  dynamic, runtime-managed jobs

Commands written to data/scheduler_commands.json by manage_scheduler.py
are picked up on each poll cycle.
"""

import html as _html
import json
import logging
import os
import random
import shutil
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Optional

import schedule

logger = logging.getLogger(__name__)

try:
    import tomli
except ImportError:
    import tomllib as tomli  # Python 3.11+


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

class Scheduler:
    """
    Manages recurring background tasks loaded from scheduler.toml and dynamic storage.
    `notify_fn` is called with a message string whenever a task with notify=True completes.
    `agent_fn`  is called with a goal string to invoke the agent for scheduled tasks.
    """

    def __init__(
        self,
        config: dict,
        notify_fn: Callable[[str], None],
        agent_fn: Optional[Callable[[str], str]] = None,
        scheduler_config_path: str = "scheduler.toml",
        data_dir: str = "data",
        long_term_memory=None,
    ):
        sched_cfg = config.get("scheduler", {})
        self.enabled: bool = sched_cfg.get("enabled", True)
        self.notify = notify_fn
        self.agent = agent_fn
        self.long_term_memory = long_term_memory
        self._data_dir = data_dir
        self._scheduler_config_path = scheduler_config_path
        self._commands_file = os.path.join(data_dir, "scheduler_commands.json")
        self._state_file = os.path.join(data_dir, "scheduler_state.json")
        self._dynamic_jobs_file = os.path.join(data_dir, "scheduled_jobs.json")

        self._jobs_meta: dict = {}
        self._run_history: dict = {}  # tag → {last_run, last_error} — persisted forever
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

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
        source: str = "dynamic",
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
        if schedule_type not in ("daily", "interval", "once"):
            return {"success": False, "error": "schedule_type must be 'daily', 'interval', or 'once'"}
        if schedule_type == "daily" and not time_str:
            return {"success": False, "error": "'time' is required for daily jobs (HH:MM)"}
        if schedule_type == "interval" and not hours and not minutes:
            return {"success": False, "error": "'hours' or 'minutes' required for interval jobs"}
        effective_run_at = run_at or time_str
        meta = {
            "tag": tag,
            "task": task,
            "schedule_type": schedule_type,
            "time": time_str,
            "run_at": effective_run_at if schedule_type == "once" else None,
            "hours": hours,
            "minutes": minutes,
            "notify": notify,
            "enabled": True,
            "source": source,
            "last_run": None,
            "created_at": datetime.utcnow().isoformat(),
        }
        self._jobs_meta[tag] = meta
        self._save_state()
        self._save_scheduler_toml()
        if self.enabled and self._thread and self._thread.is_alive():
            self.reload()
        logger.info("Job added: %s (%s)", tag, self._describe_schedule(meta))
        return {"success": True}

    def remove_job(self, tag: str) -> bool:
        tag = self._resolve_tag(tag) or tag
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
        result = []
        for tag, meta in self._jobs_meta.items():
            entry = {
                "tag": tag,
                "schedule": self._describe_schedule(meta),
                "schedule_type": meta.get("schedule_type", "interval"),
                "enabled": meta.get("enabled", True),
                "last_run": meta.get("last_run"),
                "last_error": meta.get("last_error"),
                "task": meta.get("task", ""),   # full text — display layer truncates
                "notify": meta.get("notify", True),
                "source": meta.get("source", "config"),
            }
            result.append(entry)
        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _describe_schedule(self, meta: dict) -> str:
        stype = meta.get("schedule_type", "interval")
        if stype == "daily":
            return f"daily at {meta.get('time', '?')}"
        if stype == "once":
            run_at = meta.get("run_at") or meta.get("time")
            return f"once at {run_at}" if run_at else "once (ASAP)"
        hours = meta.get("hours")
        minutes = meta.get("minutes")
        if hours:
            return f"every {hours}h"
        if minutes:
            return f"every {minutes}m"
        return "interval"

    # Maximum jitter window: ±5 minutes (in seconds)
    _JITTER_MAX_SECS = 5 * 60

    def _register_job(self, tag: str, meta: dict) -> None:
        schedule.clear(tag)
        stype = meta.get("schedule_type", "interval")
        if stype == "daily":
            t = meta.get("time", "02:00")
            schedule.every().day.at(t).do(self._run_job, tag=tag).tag(tag)
        elif stype == "once":
            run_at = meta.get("run_at") or meta.get("time")
            if run_at:
                schedule.every().day.at(run_at).do(self._run_job, tag=tag).tag(tag)
            else:
                schedule.every(1).minutes.do(self._run_job, tag=tag).tag(tag)
        else:
            hours = meta.get("hours")
            minutes = meta.get("minutes")
            if hours:
                job = schedule.every(hours).hours.do(self._run_job, tag=tag).tag(tag)
                interval_secs = int(hours) * 3600
            elif minutes:
                job = schedule.every(minutes).minutes.do(self._run_job, tag=tag).tag(tag)
                interval_secs = int(minutes) * 60
            else:
                logger.warning("Job '%s' has no interval configured — skipping", tag)
                return

            # Apply ±jitter (capped at 25% of interval or _JITTER_MAX_SECS, whichever is smaller)
            max_jitter = min(self._JITTER_MAX_SECS, interval_secs // 4)
            if max_jitter > 0:
                jitter_secs = random.randint(-max_jitter, max_jitter)
                job.next_run += timedelta(seconds=jitter_secs)
                sign = "+" if jitter_secs >= 0 else ""
                logger.debug(
                    "Registered job: %s (%s, jitter %s%ds)",
                    tag, self._describe_schedule(meta), sign, jitter_secs,
                )
                return

        logger.debug("Registered job: %s (%s)", tag, self._describe_schedule(meta))

    def _run_job(self, tag: str) -> None:
        meta = self._jobs_meta.get(tag)
        if not meta or not meta.get("enabled", True):
            return
        task = meta.get("task", "").strip()
        logger.info("Running scheduled job: %s", tag)
        now = datetime.utcnow().isoformat()
        is_once = meta.get("schedule_type") == "once"

        # --- Empty task guard ---
        if not task:
            logger.warning("Job '%s' has no task — sending direct notification", tag)
            friendly = tag.replace("_", " ")
            meta["last_run"] = now
            self._run_history[tag] = {"last_run": now, "last_error": None}
            self._save_state()
            if is_once:
                schedule.clear(tag)
                self._jobs_meta.pop(tag, None)
                self._save_state()
                self._save_scheduler_toml()  # structural change: job removed
            if meta.get("notify", True):
                self.notify(f"🔔 <b>Reminder:</b> {_html.escape(friendly)}")
            return

        result = ""
        error_occurred = False
        try:
            if self.agent:
                result = self.agent(task)
            else:
                result = "Agent not available for task."
            # Agent may return an error string (starts with ❌) — treat as error
            if result.startswith("❌"):
                error_occurred = True
        except Exception as exc:
            logger.error("Job '%s' failed: %s", tag, exc)
            result = f"Job failed: {exc}"
            error_occurred = True

        # Always update last_run (regardless of success/failure)
        meta["last_run"] = now
        if error_occurred:
            meta["last_error"] = result
        else:
            meta.pop("last_error", None)

        # Persist to run history (survives job removal and restarts)
        self._run_history[tag] = {
            "last_run": meta["last_run"],
            "last_error": meta.get("last_error"),
        }

        # Save runtime state only — last_run/last_error are not written to scheduler.toml
        self._save_state()

        if error_occurred:
            if meta.get("notify", True):
                self.notify(f"⚠️ <b>Scheduled job failed:</b> <code>{_html.escape(tag)}</code>\n\n{_html.escape(result)}")
            return

        if tag == "longterm_memory_update" and self.long_term_memory:
            try:
                self.long_term_memory.add(result, source="scheduled")
                logger.info("Long-term memory updated from job '%s'", tag)
            except Exception as exc:
                logger.warning("Failed to update long-term memory from job '%s': %s", tag, exc)

        # Auto-remove once/reminder jobs after successful execution
        if is_once:
            logger.info("Once job '%s' completed — removing", tag)
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
        except Exception as exc:
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
            except Exception as exc:
                logger.error("Error processing scheduler command %s: %s", cmd, exc)

    def _save_state(self) -> None:
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
        except Exception as exc:
            logger.warning("Could not save scheduler state: %s", exc)

    def _load_state(self) -> None:
        """Load run history from state file and overlay last_run/last_error onto active jobs."""
        if not os.path.exists(self._state_file):
            return
        try:
            with open(self._state_file) as f:
                state = json.load(f)
        except Exception as exc:
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
        except Exception as exc:
            logger.warning("Could not migrate dynamic jobs: %s", exc)

    _MAX_BACKUPS = 5

    def _prune_backups(self) -> None:
        """Keep only the last _MAX_BACKUPS backup files for scheduler.toml."""
        base = self._scheduler_config_path + ".bak."
        dir_ = os.path.dirname(os.path.abspath(self._scheduler_config_path))
        fname = os.path.basename(self._scheduler_config_path)
        prefix = fname + ".bak."
        try:
            baks = sorted(
                f for f in os.listdir(dir_) if f.startswith(prefix)
            )
            for old in baks[: max(0, len(baks) - self._MAX_BACKUPS)]:
                os.remove(os.path.join(dir_, old))
        except Exception as exc:
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
                except Exception as exc:
                    logger.warning("Failed to register job '%s' on reload: %s", tag, exc)
                    failed += 1
        logger.info("Scheduler reloaded: %d active, %d failed", reloaded, failed)
        return {"reloaded": reloaded, "failed": failed}

    def _save_scheduler_toml(self) -> None:
        """Persist all current jobs to scheduler.toml (auto-managed file)."""
        def _toml_str(v: str) -> str:
            return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'

        lines = [
            "# scheduler.toml — auto-managed by scheduler\n",
            "# Edit this file to add static jobs; dynamic/user jobs are appended here.\n",
            "\n",
        ]
        for tag, meta in self._jobs_meta.items():
            lines.append(f"[jobs.{tag}]\n")
            lines.append(f"enabled = {str(meta.get('enabled', True)).lower()}\n")
            stype = meta.get("schedule_type", "interval")
            lines.append(f'schedule = "{stype}"\n')
            if meta.get("time"):
                lines.append(f'time = "{meta["time"]}"\n')
            if meta.get("run_at"):
                lines.append(f'run_at = "{meta["run_at"]}"\n')
            if meta.get("hours") is not None:
                lines.append(f"hours = {meta['hours']}\n")
            if meta.get("minutes") is not None:
                lines.append(f"minutes = {meta['minutes']}\n")
            lines.append(f"task = {_toml_str(meta.get('task', ''))}\n")
            lines.append(f"notify = {str(meta.get('notify', True)).lower()}\n")
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
        except Exception as exc:
            logger.warning("Could not save scheduler.toml: %s", exc)

    def _load_config_jobs(self, config_path: str, sched_cfg: dict = None) -> None:
        """Load all jobs from scheduler.toml. scheduler.toml is the single source of truth."""
        if os.path.exists(config_path):
            try:
                with open(config_path, "rb") as f:
                    toml_data = tomli.load(f)
                jobs_section = toml_data.get("jobs", {})
                for tag, job_cfg in jobs_section.items():
                    stype = job_cfg.get("schedule", "interval")
                    self._jobs_meta[tag] = {
                        "tag": tag,
                        "task": job_cfg.get("task", ""),
                        "schedule_type": stype,
                        "time": job_cfg.get("time"),
                        "run_at": job_cfg.get("run_at"),
                        "hours": job_cfg.get("hours"),
                        "minutes": job_cfg.get("minutes"),
                        "notify": job_cfg.get("notify", True),
                        "enabled": job_cfg.get("enabled", True),
                        "source": job_cfg.get("source", "config"),
                        "last_run": None,
                        "created_at": job_cfg.get("created_at", datetime.utcnow().isoformat()),
                    }
                logger.info("Loaded %d jobs from %s", len(self._jobs_meta), config_path)
                return
            except Exception as exc:
                logger.warning("Could not load %s: %s — starting with no jobs", config_path, exc)
        else:
            logger.warning(
                "scheduler.toml not found at %s — no jobs loaded. "
                "Create the file to configure scheduled jobs.",
                config_path,
            )

    def _run_loop(self) -> None:
        """Poll the schedule every 30 seconds until stopped."""
        while not self._stop_event.is_set():
            self._process_pending_commands()
            schedule.run_pending()
            self._stop_event.wait(timeout=30)

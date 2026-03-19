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

import json
import logging
import os
import threading
import time
from datetime import datetime
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
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        os.makedirs(data_dir, exist_ok=True)
        self._load_config_jobs(scheduler_config_path, sched_cfg)
        self._load_dynamic_jobs()   # migration: imports old JSON, deletes it, writes to TOML
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
        if self.enabled and self._thread and self._thread.is_alive():
            self._register_job(tag, meta)
        self._save_state()
        self._save_scheduler_toml()
        logger.info("Job added: %s (%s)", tag, self._describe_schedule(meta))
        return {"success": True}

    def remove_job(self, tag: str) -> bool:
        if tag not in self._jobs_meta:
            return False
        schedule.clear(tag)
        del self._jobs_meta[tag]
        self._save_state()
        self._save_scheduler_toml()
        logger.info("Job removed: %s", tag)
        return True

    def pause_job(self, tag: str) -> bool:
        if tag not in self._jobs_meta:
            return False
        self._jobs_meta[tag]["enabled"] = False
        schedule.clear(tag)
        self._save_state()
        self._save_scheduler_toml()
        logger.info("Job paused: %s", tag)
        return True

    def resume_job(self, tag: str) -> bool:
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
        if tag not in self._jobs_meta:
            return {"success": False, "error": f"Job '{tag}' not found."}
        import threading as _threading
        _threading.Thread(target=self._run_job, kwargs={"tag": tag}, daemon=True).start()
        logger.info("Job '%s' triggered manually (run_now)", tag)
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

    def _register_job(self, tag: str, meta: dict) -> None:
        schedule.clear(tag)
        stype = meta.get("schedule_type", "interval")
        if stype == "daily":
            t = meta.get("time", "02:00")
            schedule.every().day.at(t).do(self._run_job, tag=tag).tag(tag)
        elif stype == "once":
            # Run at a specific HH:MM today (or tomorrow if that time has passed)
            run_at = meta.get("run_at") or meta.get("time")
            if run_at:
                schedule.every().day.at(run_at).do(self._run_job, tag=tag).tag(tag)
            else:
                # Run as soon as possible (next scheduler tick)
                schedule.every(1).minutes.do(self._run_job, tag=tag).tag(tag)
        else:
            hours = meta.get("hours")
            minutes = meta.get("minutes")
            if hours:
                schedule.every(hours).hours.do(self._run_job, tag=tag).tag(tag)
            elif minutes:
                schedule.every(minutes).minutes.do(self._run_job, tag=tag).tag(tag)
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
            self._save_state()
            self._save_scheduler_toml()
            if is_once:
                schedule.clear(tag)
                self._jobs_meta.pop(tag, None)
                self._save_state()
                self._save_scheduler_toml()
            if meta.get("notify", True):
                self.notify(f"🔔 <b>Reminder:</b> {friendly}")
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

        self._save_state()
        self._save_scheduler_toml()

        if error_occurred:
            if meta.get("notify", True):
                self.notify(f"⚠️ <b>Scheduled job failed:</b> <code>{tag}</code>\n\n{result}")
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
            self._save_scheduler_toml()

        if meta.get("notify", True):
            self.notify(f"📅 <b>Scheduled: {tag}</b>\n\n{result}")

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
        state = {
            "jobs": {
                tag: {
                    **meta,
                    "schedule_description": self._describe_schedule(meta),
                }
                for tag, meta in self._jobs_meta.items()
            }
        }
        tmp = self._state_file + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, self._state_file)
        except Exception as exc:
            logger.warning("Could not save scheduler state: %s", exc)

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
            os.replace(tmp, self._scheduler_config_path)
            logger.debug("Saved %d jobs to %s", len(self._jobs_meta), self._scheduler_config_path)
        except Exception as exc:
            logger.warning("Could not save scheduler.toml: %s", exc)

    def _load_config_jobs(self, config_path: str, sched_cfg: dict) -> None:
        """Load all jobs from scheduler.toml, falling back to hardcoded defaults."""
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
                logger.warning("Could not load %s: %s — using hardcoded defaults", config_path, exc)

        # Hardcoded defaults (used only when scheduler.toml is missing)
        health_time = sched_cfg.get("nightly_health_check", "02:00")
        disk_hours = sched_cfg.get("disk_check_interval_hours", 6)
        self._jobs_meta = {
            "nightly_health": {
                "tag": "nightly_health",
                "task": "Run a full system health check and summarize the status.",
                "schedule_type": "daily",
                "time": health_time,
                "run_at": None,
                "hours": None,
                "minutes": None,
                "notify": True,
                "enabled": True,
                "source": "config",
                "last_run": None,
                "created_at": datetime.utcnow().isoformat(),
            },
            "disk_check": {
                "tag": "disk_check",
                "task": "Check disk usage on all mount points. Alert if any mount point is above 80% full.",
                "schedule_type": "interval",
                "time": None,
                "run_at": None,
                "hours": disk_hours,
                "minutes": None,
                "notify": True,
                "enabled": True,
                "source": "config",
                "last_run": None,
                "created_at": datetime.utcnow().isoformat(),
            },
        }
        logger.info("Using hardcoded default scheduler jobs.")

    def _run_loop(self) -> None:
        """Poll the schedule every 30 seconds until stopped."""
        while not self._stop_event.is_set():
            self._process_pending_commands()
            schedule.run_pending()
            self._stop_event.wait(timeout=30)

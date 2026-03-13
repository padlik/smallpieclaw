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
        self._load_dynamic_jobs()
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
        hours: int = None,
        minutes: int = None,
        time_str: str = None,
        source: str = "dynamic",
    ) -> dict:
        if tag in self._jobs_meta:
            return {"success": False, "error": f"Job '{tag}' already exists."}
        if schedule_type not in ("daily", "interval"):
            return {"success": False, "error": "schedule_type must be 'daily' or 'interval'"}
        if schedule_type == "daily" and not time_str:
            return {"success": False, "error": "'time' is required for daily jobs (HH:MM)"}
        if schedule_type == "interval" and not hours and not minutes:
            return {"success": False, "error": "'hours' or 'minutes' required for interval jobs"}
        meta = {
            "tag": tag,
            "task": task,
            "schedule_type": schedule_type,
            "time": time_str,
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
        if source == "dynamic":
            self._save_dynamic_jobs()
        self._save_state()
        logger.info("Job added: %s (%s)", tag, self._describe_schedule(meta))
        return {"success": True}

    def remove_job(self, tag: str) -> bool:
        if tag not in self._jobs_meta:
            return False
        schedule.clear(tag)
        del self._jobs_meta[tag]
        self._save_dynamic_jobs()
        self._save_state()
        logger.info("Job removed: %s", tag)
        return True

    def pause_job(self, tag: str) -> bool:
        if tag not in self._jobs_meta:
            return False
        self._jobs_meta[tag]["enabled"] = False
        schedule.clear(tag)
        self._save_dynamic_jobs()
        self._save_state()
        logger.info("Job paused: %s", tag)
        return True

    def resume_job(self, tag: str) -> bool:
        if tag not in self._jobs_meta:
            return False
        self._jobs_meta[tag]["enabled"] = True
        self._register_job(tag, self._jobs_meta[tag])
        self._save_dynamic_jobs()
        self._save_state()
        logger.info("Job resumed: %s", tag)
        return True

    def list_jobs(self) -> list:
        result = []
        for tag, meta in self._jobs_meta.items():
            last_run = meta.get("last_run")
            result.append({
                "tag": tag,
                "schedule": self._describe_schedule(meta),
                "enabled": meta.get("enabled", True),
                "last_run": last_run,
                "task": meta.get("task", "")[:80],
                "source": meta.get("source", "config"),
            })
        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _describe_schedule(self, meta: dict) -> str:
        if meta.get("schedule_type") == "daily":
            return f"daily at {meta.get('time', '?')}"
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
        task = meta.get("task", "")
        logger.info("Running scheduled job: %s", tag)
        result = ""
        try:
            if self.agent:
                result = self.agent(task)
            else:
                result = f"Agent not available for task: {task}"
        except Exception as exc:
            logger.error("Job '%s' failed: %s", tag, exc)
            result = f"Job failed: {exc}"
            if meta.get("notify", True):
                self.notify(f"⚠️ Scheduled job *{tag}* failed: {exc}")
            return

        meta["last_run"] = datetime.utcnow().isoformat()
        if meta.get("source") == "dynamic":
            self._save_dynamic_jobs()
        self._save_state()

        if tag == "longterm_memory_update" and self.long_term_memory:
            try:
                self.long_term_memory.add(result, source="scheduled")
                logger.info("Long-term memory updated from job '%s'", tag)
            except Exception as exc:
                logger.warning("Failed to update long-term memory from job '%s': %s", tag, exc)

        if meta.get("notify", True):
            self.notify(f"📅 *Scheduled: {tag}*\n\n{result}")

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
        if not os.path.exists(self._dynamic_jobs_file):
            return
        try:
            with open(self._dynamic_jobs_file) as f:
                jobs = json.load(f)
            for tag, meta in jobs.items():
                meta["source"] = "dynamic"
                if tag not in self._jobs_meta:
                    self._jobs_meta[tag] = meta
        except Exception as exc:
            logger.warning("Could not load dynamic jobs: %s", exc)

    def _save_dynamic_jobs(self) -> None:
        dynamic = {
            tag: meta
            for tag, meta in self._jobs_meta.items()
            if meta.get("source") == "dynamic"
        }
        tmp = self._dynamic_jobs_file + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(dynamic, f, indent=2)
            os.replace(tmp, self._dynamic_jobs_file)
        except Exception as exc:
            logger.warning("Could not save dynamic jobs: %s", exc)

    def _load_config_jobs(self, config_path: str, sched_cfg: dict) -> None:
        """Load jobs from scheduler.toml, falling back to hardcoded defaults."""
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
                        "hours": job_cfg.get("hours"),
                        "minutes": job_cfg.get("minutes"),
                        "notify": job_cfg.get("notify", True),
                        "enabled": job_cfg.get("enabled", True),
                        "source": "config",
                        "last_run": None,
                        "created_at": datetime.utcnow().isoformat(),
                    }
                logger.info("Loaded %d jobs from %s", len(self._jobs_meta), config_path)
                return
            except Exception as exc:
                logger.warning("Could not load %s: %s — using hardcoded defaults", config_path, exc)

        # Hardcoded defaults
        health_time = sched_cfg.get("nightly_health_check", "02:00")
        disk_hours = sched_cfg.get("disk_check_interval_hours", 6)
        self._jobs_meta = {
            "nightly_health": {
                "tag": "nightly_health",
                "task": "Run a full system health check and summarize the status.",
                "schedule_type": "daily",
                "time": health_time,
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

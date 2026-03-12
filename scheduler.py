"""
scheduler.py
------------
Lightweight background task scheduler using the `schedule` library.
Runs in a daemon thread so it does not block the Telegram bot.

Predefined tasks:
  - Nightly system health check
  - Periodic disk usage alert

Results are forwarded to all authorized Telegram users.
"""

import logging
import threading
import time
from typing import Callable, Optional

import schedule

logger = logging.getLogger(__name__)


class Scheduler:
    """
    Manages recurring background tasks.
    `notify_fn` is called with a message string whenever a task completes.
    `agent_fn`  is called with a goal string to invoke the agent for scheduled tasks.
    """

    def __init__(
        self,
        config: dict,
        notify_fn: Callable[[str], None],
        agent_fn: Optional[Callable[[str], str]] = None,
    ):
        sched_cfg = config.get("scheduler", {})
        self.enabled: bool = sched_cfg.get("enabled", True)
        self.health_check_time: str = sched_cfg.get("nightly_health_check", "02:00")
        self.disk_check_hours: int = sched_cfg.get("disk_check_interval_hours", 6)
        self.notify = notify_fn
        self.agent = agent_fn
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        if not self.enabled:
            logger.info("Scheduler is disabled in config.")
            return

        self._register_jobs()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="scheduler")
        self._thread.start()
        logger.info("Scheduler started. Health check at %s daily.", self.health_check_time)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        schedule.clear()
        logger.info("Scheduler stopped.")

    def add_job(self, interval_seconds: int, fn: Callable, tag: str = "custom") -> None:
        """Add a custom recurring job at runtime."""
        schedule.every(interval_seconds).seconds.do(fn).tag(tag)
        logger.info("Custom job added: tag=%s interval=%ds", tag, interval_seconds)

    # ------------------------------------------------------------------
    # Predefined jobs
    # ------------------------------------------------------------------

    def _register_jobs(self) -> None:
        # Nightly health check
        schedule.every().day.at(self.health_check_time).do(self._job_health_check).tag("health")
        # Periodic disk check
        schedule.every(self.disk_check_hours).hours.do(self._job_disk_check).tag("disk")
        logger.debug("Scheduled jobs registered.")

    def _job_health_check(self) -> None:
        logger.info("Running scheduled nightly health check…")
        if self.agent:
            try:
                result = self.agent("Run a full system health check and summarize the status.")
                self.notify(f"🌙 *Nightly Health Check*\n\n{result}")
            except Exception as exc:
                logger.error("Health check job failed: %s", exc)
                self.notify(f"⚠️ Nightly health check failed: {exc}")
        else:
            self.notify("🌙 Nightly health check: agent not available.")

    def _job_disk_check(self) -> None:
        logger.info("Running scheduled disk usage check…")
        if self.agent:
            try:
                result = self.agent(
                    "Check disk usage on all mount points. "
                    "Alert if any mount point is above 80% full."
                )
                self.notify(f"💾 *Disk Usage Check*\n\n{result}")
            except Exception as exc:
                logger.error("Disk check job failed: %s", exc)
                self.notify(f"⚠️ Disk usage check failed: {exc}")
        else:
            self.notify("💾 Disk check: agent not available.")

    # ------------------------------------------------------------------
    # Run loop
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        """Poll the schedule every 30 seconds until stopped."""
        while not self._stop_event.is_set():
            schedule.run_pending()
            self._stop_event.wait(timeout=30)

"""
scheduler.py — Background task scheduler using APScheduler.

Runs autonomous agent goals on a cron schedule and sends results to Telegram.
"""

from __future__ import annotations
import logging
from typing import Callable, TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from agent import Agent


class Scheduler:
    def __init__(self, agent: "Agent", send_fn: Callable[[str], None]):
        """
        agent:   Agent instance to run goals against
        send_fn: callable that delivers a string message to the admin Telegram chat
        """
        self._agent = agent
        self._send = send_fn
        self._scheduler = None

    def start(self) -> None:
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.cron import CronTrigger
        except ImportError:
            logger.warning(
                "APScheduler not installed — scheduler disabled. "
                "Install with: pip install apscheduler"
            )
            return

        import config
        self._scheduler = BackgroundScheduler(timezone="UTC")

        for cron_expr, goal, job_id in config.SCHEDULER_JOBS:
            parts = cron_expr.split()
            if len(parts) != 5:
                logger.warning("Invalid cron expression '%s' for job '%s'", cron_expr, job_id)
                continue

            minute, hour, day, month, day_of_week = parts
            trigger = CronTrigger(
                minute=minute,
                hour=hour,
                day=day,
                month=month,
                day_of_week=day_of_week,
                timezone="UTC",
            )
            self._scheduler.add_job(
                self._run_job,
                trigger=trigger,
                id=job_id,
                kwargs={"goal": goal, "job_id": job_id},
                replace_existing=True,
            )
            logger.info("Scheduled job '%s': %s", job_id, cron_expr)

        self._scheduler.start()
        logger.info("Scheduler started with %d jobs.", len(config.SCHEDULER_JOBS))

    def stop(self) -> None:
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped.")

    def _run_job(self, goal: str, job_id: str) -> None:
        logger.info("Running scheduled job '%s': %s", job_id, goal)
        try:
            result = self._agent.run(goal)
            msg = (
                f"📅 *Scheduled task*: `{job_id}`\n\n"
                f"{result.answer}\n\n"
                f"_(Steps: {result.steps}, Tools: {', '.join(result.tool_calls) or 'none'})_"
            )
        except Exception as e:
            logger.error("Scheduled job '%s' failed: %s", job_id, e)
            msg = f"❌ Scheduled task `{job_id}` failed: {e}"

        try:
            self._send(msg)
        except Exception as e:
            logger.error("Could not send scheduler result to Telegram: %s", e)

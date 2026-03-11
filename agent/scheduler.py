aimport json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import config
from tool_index import index
from tool_registry import registry
from agent import Agent

config_instance = config.config

MEMORY_PATH = Path("memory.json")
# Function set by the Telegram bot – can take an optional `chat_id`
TELEGRAM_SENDER: Optional[callable] = None
# The allow‑list object – also set by the Telegram bot
SCHEDULER_ALLOWED_IDS = None   # type: ignore

def load_memory() -> Dict[str, Any]:
    if MEMORY_PATH.exists():
        return json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
    return {}

def save_memory(mem: Dict[str, Any]):
    tmp = MEMORY_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(mem, indent=2), encoding="utf-8")
    tmp.replace(MEMORY_PATH)

def _resolve_chat_id():
    # Prefer admin, otherwise the first allowed ID, otherwise none.
    if config_instance.ADMIN_ID:
        return config_instance.ADMIN_ID
    if SCHEDULER_ALLOWED_IDS and SCHEDULER_ALLOWED_IDS.list():
        return SCHEDULER_ALLOWED_IDS.list()[0]
    return None

def send_telegram(msg: str, chat_id: Optional[int] = None):
    """Send a message to Telegram. If chat_id is omitted we pick the admin / first allowed ID."""
    if TELEGRAM_SENDER is None:
        return
    target = chat_id or _resolve_chat_id()
    if target is None:
        return
    TELEGRAM_SENDER(msg, chat_id=target)

# --------------------------------------------------------------------- #
# Scheduled jobs
# --------------------------------------------------------------------- #
def nightly_health_check():
    send_telegram("Nightly health check started.")
    agent = Agent()
    goal = "Run basic system diagnostics: check disk usage and CPU temperature. Report any issues."
    response = agent.run_goal(goal, chat_id=None, steps_override=6)
    send_telegram(response[:4000])

def backup_verification():
    send_telegram("Backup verification started.")
    mem = load_memory()
    mem["last_backup_date"] = datetime.utcnow().isoformat() + "Z"
    save_memory(mem)
    send_telegram("Backup verification completed (placeholder).")

def disk_usage_monitor():
    t = registry.get_tool("check_disk")
    if t is None:
        send_telegram("check_disk tool not found.")
        return
    try:
        from tool_execution import execute_tool
        result = execute_tool("check_disk", {})
        if "usage" in result.get("stdout", "").lower() or "91%" in result.get("stdout", ""):
            send_telegram(f"Disk usage alert:\n{result['stdout'][:2000]}")
    except Exception as e:
        send_telegram(f"disk_usage_monitor error: {e}")

def start_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(nightly_health_check, CronTrigger(hour=2, minute=0))
    scheduler.add_job(backup_verification, CronTrigger(hour=1, minute=30))
    scheduler.add_job(disk_usage_monitor, CronTrigger(hour=0, minute=0))
    scheduler.start()
    return scheduler


"""
main.py — Entry point for the Telegram AI Agent.

Usage:
    python main.py

Environment variables (or edit config.py):
    TELEGRAM_TOKEN, LLM_PROVIDER, OPENAI_API_KEY, etc.
"""

from __future__ import annotations
import logging
import sys

# ── logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def main() -> None:
    import config
    from agent import Agent
    from memory import Memory
    from scheduler import Scheduler
    from security import Security
    from telegram_bot import TelegramBot
    from tool_creator import ToolCreator
    from tool_executor import ToolExecutor
    from tool_index import ToolIndex
    from tool_registry import ToolRegistry

    # ── Ensure directories exist ───────────────────────────────────────────────
    config.TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    config.TOOLS_GEN_DIR.mkdir(parents=True, exist_ok=True)

    # ── Build component graph ──────────────────────────────────────────────────
    logger.info("Initializing agent components…")

    registry = ToolRegistry()
    registry.scan()

    index = ToolIndex(registry)
    if not index.load():
        logger.info("No saved index found — building from scratch.")
        index.build()

    executor = ToolExecutor(registry)
    creator = ToolCreator(registry, index)
    memory = Memory()
    security = Security()

    agent = Agent(
        registry=registry,
        index=index,
        executor=executor,
        creator=creator,
        memory=memory,
    )

    # ── Telegram bot ───────────────────────────────────────────────────────────
    bot = TelegramBot(
        agent=agent,
        registry=registry,
        index=index,
        memory=memory,
        security=security,
    )

    # ── Scheduler ─────────────────────────────────────────────────────────────
    scheduler = Scheduler(agent=agent, send_fn=bot.send_message_sync)
    bot.set_scheduler(scheduler)
    scheduler.start()

    # ── Run ────────────────────────────────────────────────────────────────────
    logger.info("Agent ready. Starting Telegram bot polling…")
    try:
        bot.run()
    finally:
        scheduler.stop()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    main()

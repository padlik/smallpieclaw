"""
main.py
-------
Entry point for the Telegram Home Server Agent.

Boot sequence:
  1. Load config
  2. Initialise all components
  3. Build the semantic tool index
  4. Start the scheduler
  5. Start the Telegram bot (blocking)
"""

import logging
import os
import sys

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("agent.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Third-party & local imports
# ---------------------------------------------------------------------------
try:
    import tomli
except ImportError:
    import tomllib as tomli  # Python 3.11+

from agent_controller import AgentController
from llm_client import LLMClient
from memory_store import MemoryStore
from scheduler import Scheduler
from telegram_interface import TelegramInterface
from tool_creator import ToolCreator
from tool_executor import ToolExecutor
from tool_index import ToolIndex
from tool_registry import ToolRegistry


def load_config(path="config.toml"):
    if not os.path.exists(path):
        logger.error("Config file not found: %s", path)
        sys.exit(1)
    with open(path, "rb") as f:
        cfg = tomli.load(f)
    logger.info("Configuration loaded from %s", path)
    return cfg


def main():
    cfg = load_config()

    paths = cfg.get("paths", {})
    tools_dir     = paths.get("tools_dir", "tools")
    gen_tools_dir = paths.get("generated_tools_dir", "tools_generated")
    data_dir      = paths.get("data_dir", "data")
    index_path    = paths.get("tool_index_file", "data/tool_index.json")
    memory_path   = paths.get("memory_file", "data/memory.json")

    os.makedirs(tools_dir, exist_ok=True)
    os.makedirs(gen_tools_dir, exist_ok=True)
    os.makedirs(data_dir, exist_ok=True)

    agent_cfg  = cfg.get("agent", {})
    max_iter   = agent_cfg.get("max_iterations", 8)
    timeout    = agent_cfg.get("tool_timeout", 10)
    max_output = agent_cfg.get("max_output_size", 4000)
    top_tools  = agent_cfg.get("top_tools", 3)

    logger.info("Initialising components...")

    llm      = LLMClient(cfg)
    memory   = MemoryStore(memory_path)
    registry = ToolRegistry(tools_dirs=[tools_dir, gen_tools_dir])
    index    = ToolIndex(registry=registry, llm=llm, index_path=index_path)
    executor = ToolExecutor(registry=registry, timeout=timeout, max_output=max_output)
    creator  = ToolCreator(generated_dir=gen_tools_dir, registry=registry, index=index)

    agent = AgentController(
        llm=llm,
        tool_index=index,
        executor=executor,
        creator=creator,
        memory=memory,
        max_iterations=max_iter,
        top_tools=top_tools,
    )

    logger.info("Building semantic tool index...")
    try:
        index.build()
    except Exception as exc:
        logger.warning("Tool index build failed (check embeddings API config): %s", exc)

    def agent_handler(user_id, text, progress_cb):
        return agent.run(text, progress_callback=progress_cb)

    tg = TelegramInterface(cfg, agent_handler)

    def notify(msg):
        tg.send_message_to_users(msg)

    def run_agent(goal):
        return agent.run(goal)

    scheduler = Scheduler(cfg, notify_fn=notify, agent_fn=run_agent)
    scheduler.start()

    logger.info("Starting Telegram bot...")
    try:
        tg.run()
    except KeyboardInterrupt:
        logger.info("Shutdown requested.")
    finally:
        scheduler.stop()
        llm.close()
        logger.info("Agent stopped.")


if __name__ == "__main__":
    main()

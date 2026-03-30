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
import logging.handlers
import os
import sys

# Directory containing main.py — used as the agent's base directory
_AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_LOG = os.path.join(_AGENT_DIR, "agent.log")


# ---------------------------------------------------------------------------
# Nightly log rotation — Linux-style numbered suffixes
# ---------------------------------------------------------------------------
class _NightlyRotatingFileHandler(logging.handlers.TimedRotatingFileHandler):
    """
    Rotates at midnight using Linux-style numbered suffixes:
      agent.log.30 (oldest, deleted)
      agent.log.N  → agent.log.(N+1)
      agent.log.1  → agent.log.2
      agent.log    → agent.log.1   (active log renamed)
      (new empty)  → agent.log     (agent always writes here)
    """

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None

        base = self.baseFilename

        # Shift numbered backups: .N → .(N+1), oldest removed
        for i in range(self.backupCount - 1, 0, -1):
            src = f"{base}.{i}"
            dst = f"{base}.{i + 1}"
            if os.path.exists(dst):
                os.remove(dst)
            if os.path.exists(src):
                os.rename(src, dst)

        # Rotate active log: agent.log → agent.log.1
        dst1 = f"{base}.1"
        if os.path.exists(dst1):
            os.remove(dst1)
        if os.path.exists(base):
            os.rename(base, dst1)

        # Open fresh agent.log (always the active log)
        self.mode = "a"
        self.stream = self._open()

        # Advance the next rollover time
        self.rolloverAt += self.interval


def _setup_logging(log_file: str = _DEFAULT_LOG, backup_count: int = 30) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    stream_h = logging.StreamHandler(sys.stdout)
    stream_h.setFormatter(fmt)

    # Rotate at 00:00 local time; keep last backup_count daily files.
    # Active log is always log_file; rotated copies become .1, .2, …
    file_h = _NightlyRotatingFileHandler(
        log_file,
        when="midnight",
        interval=1,
        backupCount=backup_count,
        encoding="utf-8",
        utc=False,
    )
    file_h.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(stream_h)
    root.addHandler(file_h)

    # Suppress high-volume INFO noise from HTTP/Telegram internals
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext").setLevel(logging.WARNING)


# Bootstrap with default path; reconfigured after config load if needed
_setup_logging()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Third-party & local imports
# ---------------------------------------------------------------------------
try:
    import tomli
except ImportError:
    import tomllib as tomli  # Python 3.11+

from agent_controller import AgentController
from builtin_executor import BuiltinExecutor
from llm_client import LLMClient
from memory_store import MemoryStore, ShortTermMemory, WorkingMemory, LongTermMemory, ResultsMemory
from scheduler import Scheduler
from skill_registry import SkillRegistry
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
    longterm_path = paths.get("longterm_memory_file", "data/longterm_memory.json")
    results_path  = paths.get("results_memory_file", "data/results_memory.json")
    scheduler_config_path = paths.get("scheduler_config", "scheduler.toml")
    skills_dir    = paths.get("skills_dir", "skills")
    downloads_dir = os.path.abspath(paths.get("downloads_dir", "downloads"))
    _agent_name   = os.path.basename(os.path.abspath("."))
    tmp_dir       = os.path.abspath(paths.get("tmp_dir", f"/tmp/{_agent_name}"))
    log_file         = paths.get("log_file", _DEFAULT_LOG)
    log_backup_count = int(paths.get("log_backup_count", 30))

    # Re-initialise logging with the configured path (replaces the bootstrap handler)
    for h in logging.root.handlers[:]:
        logging.root.removeHandler(h)
        h.close()
    _setup_logging(log_file, backup_count=log_backup_count)

    os.makedirs(tools_dir, exist_ok=True)
    os.makedirs(gen_tools_dir, exist_ok=True)
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(skills_dir, exist_ok=True)
    os.makedirs(downloads_dir, exist_ok=True)
    os.makedirs(tmp_dir, exist_ok=True)
    logger.info("Downloads dir: %s | Tmp dir: %s", downloads_dir, tmp_dir)

    agent_cfg  = cfg.get("agent", {})
    max_iter   = agent_cfg.get("max_iterations", 8)
    timeout    = agent_cfg.get("tool_timeout", 10)
    max_output = agent_cfg.get("max_output_size", 4000)
    top_tools  = agent_cfg.get("top_tools", 3)
    ctx_max_tokens = agent_cfg.get("ctx_max_tokens", 90_000)

    logger.info("Initialising components...")

    llm      = LLMClient(cfg)
    memory   = MemoryStore(memory_path)
    registry = ToolRegistry(tools_dirs=[tools_dir, gen_tools_dir])
    builtin  = BuiltinExecutor(default_timeout=timeout, max_output=max_output)
    index    = ToolIndex(registry=registry, llm=llm, index_path=index_path, builtin_executor=builtin)
    executor = ToolExecutor(registry=registry, timeout=timeout, max_output=max_output)
    creator  = ToolCreator(generated_dir=gen_tools_dir, registry=registry, index=index)

    short_term  = ShortTermMemory(max_turns=20)
    working     = WorkingMemory()
    long_term   = LongTermMemory(path=longterm_path, llm=llm)
    results_mem = ResultsMemory(path=results_path, llm=llm)

    skills = SkillRegistry(skills_dir=skills_dir)
    logger.info("Loaded %d skill(s) from %s", skills.count(), skills_dir)

    agent = AgentController(
        llm=llm,
        tool_index=index,
        executor=executor,
        creator=creator,
        memory=memory,
        max_iterations=max_iter,
        top_tools=top_tools,
        ctx_max_tokens=ctx_max_tokens,
        short_term=short_term,
        working=working,
        long_term=long_term,
        results=results_mem,
        builtin_executor=builtin,
        skill_registry=skills,
        tmp_dir=tmp_dir,
        downloads_dir=downloads_dir,
    )

    logger.info("Building semantic tool index...")
    try:
        index.build()
    except Exception as exc:
        logger.warning("Tool index build failed (check embeddings API config): %s", exc)

    def agent_handler(user_id, text, progress_cb):
        return agent.run(text, progress_callback=progress_cb)

    def run_agent(goal):
        return agent.run(goal)

    # Build TelegramInterface first so notify() can reference it
    # (scheduler and tg are wired together via forward references in closures)
    _tg_holder: list = [None]

    def notify(msg):
        if _tg_holder[0] is not None:
            _tg_holder[0].send_message_to_users(msg)

    scheduler = Scheduler(
        cfg, notify_fn=notify, agent_fn=run_agent,
        scheduler_config_path=scheduler_config_path,
        data_dir=data_dir,
        long_term_memory=long_term,
    )
    builtin.scheduler = scheduler  # wire scheduler into built-in tool

    logger.info("Starting Telegram bot...")
    tg = TelegramInterface(
        cfg, agent_handler,
        agent_reset_fn=agent.reset_task,
        scheduler=scheduler,
        tool_registry=registry,
        llm_client=llm,
        tool_index=index,
        skill_registry=skills,
    )
    tg.agent = agent  # wire agent for confirm/resume and /models
    _tg_holder[0] = tg

    scheduler.start()
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

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

from __future__ import annotations

import fcntl
import logging
import logging.handlers
import os
import signal
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
# PID file locking — prevents multiple concurrent instances
# ---------------------------------------------------------------------------
class _PidFileLock:
    """
    Advisory exclusive lock on a PID file using fcntl.flock().

    The OS releases the lock automatically when the process exits (even on
    crash), so stale PID files left by a previous crash are handled without
    any PID-alive check — if flock() succeeds, the previous holder is gone.

    Usage::

        with _PidFileLock("/run/agent.pid"):
            ...  # only one process can hold this at a time
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._fd: int | None = None

    def __enter__(self) -> "_PidFileLock":
        os.makedirs(os.path.dirname(os.path.abspath(self._path)), exist_ok=True)
        try:
            fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o644)
        except OSError as exc:
            logger.error("Cannot open PID file %s: %s", self._path, exc)
            sys.exit(1)

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                existing_pid = open(self._path).read().strip()
            except OSError:
                existing_pid = "unknown"
            logger.error(
                "Another instance is already running (PID %s). Aborting.", existing_pid
            )
            os.close(fd)
            sys.exit(1)

        # Lock acquired — write our PID
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd
        return self

    def __exit__(self, *_) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        try:
            os.unlink(self._path)
        except OSError:
            pass

# ---------------------------------------------------------------------------
# Third-party & local imports
# ---------------------------------------------------------------------------
try:
    import tomli
except ImportError:
    import tomllib as tomli  # Python 3.11+

from agent_controller import AgentController, SubAgentRunner  # noqa: E402
from builtin_executor import BuiltinExecutor, _load_context  # noqa: E402
from llm_client import LLMClient  # noqa: E402
from mcp_client import MCPManager  # noqa: E402
from memory_store import MemoryStore, ShortTermMemory, WorkingMemory, LongTermMemory, ResultsMemory  # noqa: E402
from scheduler import Scheduler  # noqa: E402
from skill_registry import SkillRegistry  # noqa: E402
from telegram_interface import TelegramInterface  # noqa: E402
from token_usage import get_registry as get_token_registry  # noqa: E402
from tool_creator import ToolCreator  # noqa: E402
from tool_executor import ToolExecutor  # noqa: E402
from tool_index import ToolIndex  # noqa: E402
from tool_registry import ToolRegistry  # noqa: E402


def load_config(path="config.toml"):
    if not os.path.exists(path):
        logger.error("Config file not found: %s", path)
        sys.exit(1)
    with open(path, "rb") as f:
        cfg = tomli.load(f)
    logger.info("Configuration loaded from %s", path)

    # Validate config structure early — fail fast with clear error messages
    from config_schema import parse_config
    from exceptions import ConfigError
    try:
        parse_config(cfg)
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        sys.exit(1)

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
    pid_file      = os.path.join(
        _AGENT_DIR,
        paths.get("pid_file", os.path.join(data_dir, "agent.pid")),
    ) if not os.path.isabs(paths.get("pid_file", "")) else paths["pid_file"]

    # SIGTERM → clean exit so the finally: block always runs (e.g. systemctl stop)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    with _PidFileLock(pid_file):
        _run(
            cfg=cfg, paths=paths,
            tools_dir=tools_dir, gen_tools_dir=gen_tools_dir, data_dir=data_dir,
            index_path=index_path, memory_path=memory_path,
            longterm_path=longterm_path, results_path=results_path,
            scheduler_config_path=scheduler_config_path, skills_dir=skills_dir,
            downloads_dir=downloads_dir, tmp_dir=tmp_dir,
            log_file=log_file, log_backup_count=log_backup_count,
        )


def _run(
    cfg, paths,
    tools_dir, gen_tools_dir, data_dir,
    index_path, memory_path, longterm_path, results_path,
    scheduler_config_path, skills_dir,
    downloads_dir, tmp_dir,
    log_file, log_backup_count,
):
    """Core startup after PID lock is acquired."""
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
    max_subagents = agent_cfg.get("max_subagents", 6)
    subagent_result_timeout = agent_cfg.get("subagent_result_timeout", 300)
    # Separate step cap for scheduled/background agents (chat sessions use max_iter)
    _raw_sched_max = agent_cfg.get("scheduled_max_iterations", 100)
    scheduled_max_iter = min(_raw_sched_max, 500) if _raw_sched_max > 0 else 500

    logger.info("Initialising components...")

    llm      = LLMClient(cfg, usage_registry=get_token_registry(), caller_tag="main")
    memory   = MemoryStore(memory_path)
    # Purge any model/LLM facts the agent may have written in past sessions.
    # These are always stale — authoritative model info lives in config.toml and is
    # injected fresh into every system prompt via _format_models().
    _purged = memory.purge_matching("model", "llm")
    if _purged:
        logger.info("Startup: purged %d stale model/provider key(s) from memory store", _purged)
    registry = ToolRegistry(tools_dirs=[tools_dir, gen_tools_dir])
    builtin  = BuiltinExecutor(
        default_timeout=timeout, max_output=max_output, data_dir=data_dir, memory=memory,
        max_subagents=max_subagents, subagent_result_timeout=subagent_result_timeout,
    )
    index    = ToolIndex(registry=registry, llm=llm, index_path=index_path, builtin_executor=builtin)
    executor = ToolExecutor(registry=registry, timeout=timeout, max_output=max_output)
    creator  = ToolCreator(generated_dir=gen_tools_dir, registry=registry, index=index)

    # Initialise MCP servers (optional — skip if none configured)
    mcp_manager: MCPManager | None = None
    mcp_server_cfgs = cfg.get("mcp_servers", [])
    if mcp_server_cfgs:
        logger.info("Initialising %d MCP server(s)...", len(mcp_server_cfgs))
        mcp_manager = MCPManager(mcp_server_cfgs)
        try:
            mcp_manager.connect_all()
            mcp_tools = mcp_manager.get_tools()
            if mcp_tools:
                # Group by server and register
                servers_seen: set[str] = set()
                for t in mcp_tools:
                    servers_seen.add(t.server_name)
                for srv in servers_seen:
                    srv_tools = [t for t in mcp_tools if t.server_name == srv]
                    registry.register_mcp_tools(srv, srv_tools)
                logger.info("MCP: registered %d tool(s) from %d server(s)",
                            len(mcp_tools), len(servers_seen))
        except Exception as exc:
            logger.warning("MCP connect_all failed (agent will start without MCP): %s", exc)

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
        mcp_manager=mcp_manager,
        tmp_dir=tmp_dir,
        downloads_dir=downloads_dir,
        log_file=log_file,
        log_backup_count=log_backup_count,
    )

    logger.info("Building semantic tool index...")
    try:
        index.build()
    except Exception as exc:
        logger.warning("Tool index build failed (check embeddings API config): %s", exc)

    def agent_handler(user_id, text, progress_cb, images=None):
        return agent.run(text, progress_callback=progress_cb, images=images or None)

    # Build TelegramInterface first so notify() can reference it
    # (tg is created after scheduler/sub_agent_factory wiring, so we use nonlocal)
    tg: TelegramInterface | None = None

    def notify(msg):
        if tg is not None:
            tg.send_message_to_users(msg)

    def notify_html(html_msg):
        if tg is not None:
            tg.send_html_to_users(html_msg)

    # Resolve background_model for sub-agents
    background_model_id = agent_cfg.get("background_model") or agent_cfg.get("default_model", "")
    all_models = cfg.get("models", [])
    background_model_cfg = next(
        (m for m in all_models if m.get("model") == background_model_id),
        all_models[0] if all_models else {},
    )
    if background_model_id and background_model_cfg.get("model") != background_model_id:
        logger.warning(
            "background_model '%s' not found in [[models]]. Falling back to '%s'.",
            background_model_id,
            background_model_cfg.get("model", "none"),
        )

    def sub_agent_factory(model=None, context_key=None, label="on-demand", notify_fn=None,
                          fallback_models=None, max_iterations=None):
        """Create an isolated SubAgentRunner with the requested model override."""
        # Resolve model config
        if model:
            model_cfg = next((m for m in all_models if m.get("model") == model), None)
            if model_cfg is None:
                raise ValueError(
                    f"Model '{model}' not found in [[models]]. "
                    f"Available: {[m.get('model') for m in all_models]}"
                )
        else:
            model_cfg = background_model_cfg

        # max_iterations: explicit override > scheduled default (never use chat max_iter here)
        effective_max_iter = max_iterations if max_iterations is not None else scheduled_max_iter

        ctx_max_turns = 50
        pre_loaded_ctx = None
        if context_key:
            pre_loaded_ctx = _load_context(context_key, data_dir, max_turns=ctx_max_turns)

        runner = SubAgentRunner(
            model_cfg=model_cfg,
            config=cfg,
            tool_index=index,
            executor=executor,
            creator=creator,
            base_memory=memory,
            builtin_executor=builtin,
            skill_registry=skills,
            mcp_manager=mcp_manager,
            long_term=long_term,
            results=results_mem,
            short_term=pre_loaded_ctx,
            notify_fn=notify_fn or notify,
            context_key=context_key,
            label=label,
            max_iterations=effective_max_iter,
            top_tools=top_tools,
            ctx_max_tokens=ctx_max_tokens,
            tmp_dir=tmp_dir,
            downloads_dir=downloads_dir,
            usage_registry=get_token_registry(),
            depth=1,
            fallback_models=fallback_models,
        )
        return runner

    # Wire sub_agent_factory into builtin executor
    builtin._sub_agent_factory = sub_agent_factory
    builtin._notify_html_fn = notify_html

    scheduler = Scheduler(
        cfg, notify_fn=notify,
        scheduler_config_path=scheduler_config_path,
        data_dir=data_dir,
        long_term_memory=long_term,
        builtin_executor=builtin,
    )
    builtin.scheduler = scheduler  # wire scheduler into built-in tool

    logger.info("Starting Telegram bot...")
    tg = TelegramInterface(
        cfg, agent_handler,
        agent_reset_fn=agent.reset_task,
        agent_compress_fn=agent.compress_context,
        scheduler=scheduler,
        tool_registry=registry,
        llm_client=llm,
        tool_index=index,
        skill_registry=skills,
        usage_registry=get_token_registry(),
        downloads_dir=downloads_dir,
        mcp_manager=mcp_manager,
    )
    tg.agent = agent  # wire agent for confirm/resume and /models

    scheduler.start()
    try:
        tg.run()
    except KeyboardInterrupt:
        logger.info("Shutdown requested.")
    finally:
        scheduler.stop()
        builtin.shutdown()
        llm.close()
        if mcp_manager:
            mcp_manager.close_all()
            logger.info("MCP servers closed.")
        logger.info("Agent stopped.")


if __name__ == "__main__":
    main()

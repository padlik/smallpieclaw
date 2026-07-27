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
import os
import shutil
import signal
import sys
import tempfile

import agent_logging

# Directory containing main.py — used as the agent's base directory
_AGENT_DIR = os.path.dirname(os.path.abspath(__file__))


# Bootstrap logging to stdout until config (and the resolved XDG log path) load.
agent_logging.setup_bootstrap()
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

from agent_controller import AgentController  # noqa: E402
from agent_runtime import AgentRuntime, RuntimeOptions, RuntimeProfile  # noqa: E402
from builtin_executor import BuiltinExecutor  # noqa: E402
from config_schema import resolve_model_id, vault_path, log_path, parse_vault_content  # noqa: E402
from graph_memory import create_graph_memory  # noqa: E402
from llm_client import LLMClient  # noqa: E402
from mcp_client import MCPManager  # noqa: E402
from memory_store import MemoryStore, ShortTermMemory, WorkingMemory, ResultsMemory  # noqa: E402
from prompt_registry import PromptRegistry  # noqa: E402
from scheduler import Scheduler  # noqa: E402
from skill_registry import SkillRegistry  # noqa: E402
from strategy_memory import StrategyMemory  # noqa: E402
from telegram_interface import TelegramInterface  # noqa: E402
from token_usage import get_registry as get_token_registry  # noqa: E402
from tool_index import ToolIndex  # noqa: E402
from tool_registry import ToolRegistry  # noqa: E402


def load_config(path="config.toml"):
    if not os.path.exists(path):
        logger.error("Config file not found: %s", path)
        sys.exit(1)
    with open(path, "rb") as f:
        cfg = tomli.load(f)
    logger.info("Configuration loaded from %s", path)

    # Validate config structure early — fail fast with clear error messages.
    # parse_config() also expands ${VAR}/${VAR:-default} placeholders; we return
    # the resolved dict so all runtime consumers see plain values, not literals.
    # The typed AppConfig is returned too so callers never need to re-parse (a
    # second parse would re-scan already-substituted values, which is wrong).
    from config_schema import parse_config
    from exceptions import ConfigError
    try:
        app_cfg = parse_config(cfg)
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        sys.exit(1)

    return app_cfg._raw, app_cfg


def _read_vault_secrets(vault_file: str) -> list[str]:
    """Return all string values stored in the vault, for log redaction.

    Best-effort: returns an empty list if the vault is absent or unreadable so
    logging setup never fails on account of the vault.
    """
    try:
        if not os.path.exists(vault_file):
            return []
        with open(vault_file, encoding="utf-8") as f:
            vault = parse_vault_content(f.read(), vault_file, require_all_strings=False)
        return [v for v in vault.values() if isinstance(v, str) and v]
    except Exception as exc:  # noqa: BLE001 — redaction is best-effort
        logger.warning("Could not read vault secrets for log redaction: %s", exc)
        return []


def main():
    cfg, app_cfg = load_config()

    paths = cfg.get("paths", {})
    data_dir      = paths.get("data_dir", "data")
    index_path    = paths.get("tool_index_file", "data/tool_index.json")
    memory_path   = paths.get("memory_file", "data/memory.json")
    results_path  = paths.get("results_memory_file", "data/results_memory.json")
    scheduler_config_path = paths.get("scheduler_config", "scheduler.toml")
    skills_dir    = paths.get("skills_dir", "skills")
    downloads_dir = os.path.abspath(paths.get("downloads_dir", "downloads"))
    _agent_name   = os.path.basename(os.path.abspath("."))
    tmp_dir       = os.path.abspath(paths.get("tmp_dir", f"/tmp/{_agent_name}"))
    workspace_dir = os.path.abspath(os.path.expanduser(paths.get("workspace_dir", "~/Documents")))
    log_file         = log_path(cfg)
    log_backup_count = int(paths.get("log_backup_count", 30))
    pid_file      = os.path.join(
        _AGENT_DIR,
        paths.get("pid_file", os.path.join(data_dir, "agent.pid")),
    ) if not os.path.isabs(paths.get("pid_file", "")) else paths["pid_file"]

    # SIGTERM → clean exit so the finally: block always runs (e.g. systemctl stop)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    with _PidFileLock(pid_file):
        _run(
            cfg=cfg, app_cfg=app_cfg, paths=paths,
            data_dir=data_dir,
            index_path=index_path, memory_path=memory_path,
            results_path=results_path,
            scheduler_config_path=scheduler_config_path, skills_dir=skills_dir,
            downloads_dir=downloads_dir, tmp_dir=tmp_dir,
            workspace_dir=workspace_dir,
            log_file=log_file, log_backup_count=log_backup_count,
            agent_name=os.path.basename(os.path.abspath(".")),
        )


def _run(
    cfg, app_cfg, paths,
    data_dir,
    index_path, memory_path, results_path,
    scheduler_config_path, skills_dir,
    downloads_dir, tmp_dir, workspace_dir,
    log_file, log_backup_count,
    agent_name,
):
    """Core startup after PID lock is acquired."""
    # Re-initialise logging with the configured XDG path and structlog dual sink.
    # Vault secret values are passed so they are redacted from every log record.
    json_log_path = agent_logging.setup_logging(
        log_file,
        backup_count=log_backup_count,
        secret_values=_read_vault_secrets(vault_path(cfg)),
    )

    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(skills_dir, exist_ok=True)
    os.makedirs(downloads_dir, exist_ok=True)
    os.makedirs(tmp_dir, exist_ok=True)
    # XDG state dir for trusted_dirs.json — outside the nsjail-mounted project dir
    # so a sandboxed shell command cannot overwrite the trust store.
    xdg_state_home = os.environ.get(
        "XDG_STATE_HOME", os.path.join(os.path.expanduser("~"), ".local", "state")
    )
    nsjail_state_dir = os.path.join(xdg_state_home, agent_name)
    os.makedirs(nsjail_state_dir, exist_ok=True)
    trusted_dirs_path = os.path.join(nsjail_state_dir, "trusted_dirs.json")
    # One-time migration from old location
    old_trusted = os.path.join(data_dir, "trusted_dirs.json")
    if os.path.exists(old_trusted) and not os.path.exists(trusted_dirs_path):
        shutil.copy2(old_trusted, trusted_dirs_path)
        logger.info("Migrated trusted_dirs.json to XDG state: %s", trusted_dirs_path)
    os.environ["TMPDIR"] = tmp_dir
    os.environ["TMP"] = tmp_dir
    os.environ["TEMP"] = tmp_dir
    logger.info("Downloads dir: %s | Tmp dir: %s", downloads_dir, tmp_dir)

    agent_cfg  = cfg.get("agent", {})
    max_iter   = agent_cfg.get("max_iterations", 8)
    timeout    = agent_cfg.get("tool_timeout", 10)
    max_output = agent_cfg.get("max_output_size", 4000)
    top_tools  = agent_cfg.get("top_tools", 3)
    ctx_max_tokens = agent_cfg.get("ctx_max_tokens", 90_000)
    max_subagents = agent_cfg.get("max_subagents", 6)
    subagent_result_timeout = agent_cfg.get("subagent_result_timeout", 300)
    shell_backend = agent_cfg.get("shell_backend", "subprocess")
    shell_pty_cols = int(agent_cfg.get("shell_pty_cols", 220))
    shell_pty_rows = int(agent_cfg.get("shell_pty_rows", 50))
    shell_streaming = bool(agent_cfg.get("shell_streaming", False))
    shell_nsjail_confirm_mode = agent_cfg.get("shell_nsjail_confirm_mode", "always")
    shell_nsjail_memory_mb = int(agent_cfg.get("shell_nsjail_memory_mb", 256))
    shell_nsjail_pids_max = int(agent_cfg.get("shell_nsjail_pids_max", 64))
    shell_nsjail_cpu_percent = int(agent_cfg.get("shell_nsjail_cpu_percent", 50))
    shell_nsjail_network = agent_cfg.get("shell_nsjail_network", "none")
    creativity_mode = agent_cfg.get("creativity_mode", "default")
    plan_max_iterations = int(agent_cfg.get("plan_max_iterations", 50))
    inactivity_warn_minutes = int(agent_cfg.get("inactivity_warn_minutes", 15))
    # Separate step cap for scheduled/background agents (chat sessions use max_iter)
    _raw_sched_max = agent_cfg.get("scheduled_max_iterations", 100)
    scheduled_max_iter = min(_raw_sched_max, 500) if _raw_sched_max > 0 else 500

    logger.info("Initialising components...")

    # Per-session temp dir for nsjail /tmp bind mount — persists across nsjail
    # invocations within a session, cleaned up at agent shutdown.
    nsjail_session_tmpdir = tempfile.mkdtemp(prefix="nsjail-tmp-")
    logger.info("nsjail session tmpdir: %s", nsjail_session_tmpdir)

    vault_file = vault_path(cfg)

    llm      = LLMClient(cfg, usage_registry=get_token_registry(), caller_tag="main")
    memory   = MemoryStore(memory_path)
    # Purge any model/LLM facts the agent may have written in past sessions.
    # These are always stale — authoritative model info lives in config.toml and is
    # injected fresh into every system prompt via _format_models().
    _purged = memory.purge_matching("model", "llm")
    if _purged:
        logger.info("Startup: purged %d stale model/provider key(s) from memory store", _purged)
    registry = ToolRegistry()
    builtin  = BuiltinExecutor(
        default_timeout=timeout, max_output=max_output, data_dir=data_dir, memory=memory,
        max_subagents=max_subagents, subagent_result_timeout=subagent_result_timeout,
        shell_backend=shell_backend, shell_pty_cols=shell_pty_cols, shell_pty_rows=shell_pty_rows,
        shell_streaming=shell_streaming, vault_path=vault_file, log_jsonl_path=json_log_path,
        shell_nsjail_confirm_mode=shell_nsjail_confirm_mode,
        shell_nsjail_memory_mb=shell_nsjail_memory_mb,
        shell_nsjail_pids_max=shell_nsjail_pids_max,
        shell_nsjail_cpu_percent=shell_nsjail_cpu_percent,
        shell_nsjail_network=shell_nsjail_network,
        nsjail_session_tmpdir=nsjail_session_tmpdir,
        nsjail_project_dir=_AGENT_DIR,
        nsjail_trusted_dirs_path=trusted_dirs_path,
    )
    index    = ToolIndex(registry=registry, llm=llm, index_path=index_path, builtin_executor=builtin)

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

    short_term    = ShortTermMemory(max_turns=20)
    working       = WorkingMemory()
    results_mem   = ResultsMemory(path=results_path, llm=llm)
    strategy_mem  = StrategyMemory(data_dir=data_dir)
    # Wire working memory and results memory into builtin executor so that
    # implicit spawn_agent context summaries include recent tool results.
    builtin._working = working
    builtin._results = results_mem

    # Prompt registry singleton: tracks monotonic "Prompt #N" runs.
    prompt_registry = PromptRegistry(data_dir=data_dir)
    builtin._prompt_registry = prompt_registry
    from builtin_tools.access_control import TrustedZoneChecker as _TrustedZoneChecker
    _trusted_zone_checker = _TrustedZoneChecker(
        paths_config=app_cfg.paths,
        data_dir=data_dir,
        agent_name=app_cfg.agent.agent_name,
        vault_path=vault_file,
        trusted_dirs_path=trusted_dirs_path,
    )
    builtin.trusted_zone_checker = _trusted_zone_checker
    # NOTE: JSON LongTermMemory is no longer constructed or wired into runtime
    # agents (P2 consolidation). Runtime semantic recall is served by graph
    # memory; the legacy JSON store is migration/backfill-only via
    # backfill_graph_memory.py.

    skills = SkillRegistry(skills_dir=skills_dir)
    logger.info("Loaded %d skill(s) from %s", skills.count(), skills_dir)

    agent = AgentController(
        llm=llm,
        tool_index=index,
        memory=memory,
        max_iterations=max_iter,
        top_tools=top_tools,
        ctx_max_tokens=ctx_max_tokens,
        short_term=short_term,
        working=working,
        results=results_mem,
        builtin_executor=builtin,
        skill_registry=skills,
        mcp_manager=mcp_manager,
        tmp_dir=tmp_dir,
        downloads_dir=downloads_dir,
        workspace_dir=workspace_dir,
        log_file=log_file,
        log_backup_count=log_backup_count,
        creativity_mode=creativity_mode,
        plan_max_iterations=plan_max_iterations,
        inactivity_warn_minutes=inactivity_warn_minutes,
    )

    # Wire strategy memory into the agent so the ReAct loop can query learned
    # approaches in a later iteration. Assigned post-construction to avoid
    # changing the AgentController signature today.
    agent.strategy_memory = strategy_mem  # type: ignore[attr-defined]
    agent.trusted_zone_checker = _trusted_zone_checker  # type: ignore[attr-defined]

    logger.info("Building semantic tool index...")
    try:
        index.build()
    except Exception as exc:
        logger.warning("Tool index build failed (check embeddings API config): %s", exc)

    def agent_handler(user_id, text, progress_cb, images=None, *, prompt_id=None, trace_id=None):
        return agent.run(text, progress_callback=progress_cb, images=images or None,
                         prompt_id=prompt_id, trace_id=trace_id)

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
    _bg_resolved = resolve_model_id(background_model_id, all_models)
    background_model_cfg = next(
        (m for m in all_models if m.get("model") == _bg_resolved),
        all_models[0] if all_models else {},
    )
    if background_model_id and background_model_cfg.get("model") != _bg_resolved:
        logger.warning(
            "background_model '%s' not found in [[models]]. Falling back to '%s'.",
            background_model_id,
            background_model_cfg.get("model", "none"),
        )

    # Construction boundary for all sub-agent products (ADR-0007). The runtime
    # holds the shared, run-independent collaborators; per-call construction
    # knobs are passed through RuntimeOptions. Construction is uniform across the
    # sub-agent profiles — the visibility source (on-demand/scheduled/plan-step/
    # diagnostic) is assigned later by register_run, not by the runtime.
    agent_runtime = AgentRuntime(
        config=cfg,
        all_models=all_models,
        background_model_cfg=background_model_cfg,
        tool_index=index,
        base_memory=memory,
        builtin_executor=builtin,
        skill_registry=skills,
        mcp_manager=mcp_manager,
        results=results_mem,
        usage_registry=get_token_registry(),
        notify_fn=notify,
        data_dir=data_dir,
        tmp_dir=tmp_dir,
        downloads_dir=downloads_dir,
        workspace_dir=workspace_dir,
        top_tools=top_tools,
        ctx_max_tokens=ctx_max_tokens,
        scheduled_max_iterations=scheduled_max_iter,
    )

    def sub_agent_factory(model=None, context_key=None, label="on-demand", notify_fn=None,
                          fallback_models=None, max_iterations=None,
                          max_tokens=None, temperature=None, top_p=None,
                          on_tool_trace=None, cancel_event=None, trace_id=None,
                          context_payload=None, prompt_variant=None,
                          runtime_profile=RuntimeProfile.ON_DEMAND_SUBAGENT):
        """Create an isolated SubAgentRunner with the requested model override.

        Thin frontend over ``AgentRuntime.create``; the signature is preserved for
        existing callers (``BuiltinExecutor.spawn_agent``, the scheduler, and
        ``PlanExecutor``).

        ``runtime_profile`` selects the construction profile (default
        ``ON_DEMAND_SUBAGENT``); scheduler, plan-step, and diagnostic call sites
        pass their matching profile through this internal parameter. Construction
        is behavior-equivalent across the sub-agent profiles today — the profile
        is threaded so the visibility source assigned later stays consistent with
        the construction origin.
        """
        options = RuntimeOptions(
            model=model,
            fallback_models=fallback_models,
            max_iterations=max_iterations,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            context_key=context_key,
            context_payload=context_payload,
            prompt_variant=prompt_variant,
            trace_id=trace_id,
            cancel_event=cancel_event,
            label=label,
        )
        return agent_runtime.create(
            runtime_profile,
            options,
            notify_fn=notify_fn,
            on_tool_trace=on_tool_trace,
        )

    # Wire sub_agent_factory into builtin executor
    builtin._sub_agent_factory = sub_agent_factory
    builtin._notify_html_fn = notify_html

    scheduler = Scheduler(
        cfg, notify_fn=notify,
        scheduler_config_path=scheduler_config_path,
        data_dir=data_dir,
        builtin_executor=builtin,
    )
    builtin.scheduler = scheduler  # wire scheduler into built-in tool
    # Give the main agent access to scheduled job execution history
    agent._job_history_fn = scheduler.execution_log.format_for_prompt

    # Initialise graph memory (opt-in — only when enabled in config and ladybug installed)
    graph_memory_store = None
    graph_memory_writer = None
    try:
        if app_cfg.graph_memory.enabled:
            from graph_memory import build_extraction_llm_call as _build_extraction_llm_call

            def _embedder_fn(text: str) -> list[float]:
                return llm.embed(text)

            _llm_call_fn = _build_extraction_llm_call(cfg, app_cfg, all_models)

            graph_memory_store, graph_memory_writer = create_graph_memory(
                cfg=app_cfg,
                embedder_fn=_embedder_fn,
                llm_call_fn=_llm_call_fn,
                embedding_dim=len(llm.embed("test")),
            )
            if graph_memory_store is not None:
                # Wire into agent (main react_loop context)
                agent._graph_memory = graph_memory_store
                agent._graph_memory_writer = graph_memory_writer
                agent._graph_memory_max_entries = app_cfg.graph_memory.max_context_entries
                # Wire into builtin executor for memory_graph_search/store tools
                builtin._graph_memory = graph_memory_store
                builtin._graph_memory_writer = graph_memory_writer
                logger.info(
                    "Graph memory enabled (db=%s, extraction_model=%s)",
                    app_cfg.graph_memory.db_path,
                    app_cfg.graph_memory.extraction_model or app_cfg.agent.default_model,
                )
    except Exception as _gm_init_exc:
        logger.warning("Graph memory initialisation failed (continuing without it): %s", _gm_init_exc)

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
    tg._graph_memory_store = graph_memory_store
    tg._graph_memory_writer = graph_memory_writer
    tg._prompt_registry = prompt_registry  # wire prompt registry for /prompts and lifecycle

    # Wire the sub-agent Telegram confirmation bridge into the built-in executor.
    # Sub-agents running sensitive file operations will call this to ask the
    # operator for approval via inline buttons before executing.
    builtin._subagent_confirm_prompt_fn = tg.send_subagent_confirmation_prompt

    scheduler.start()
    try:
        tg.run()
    except KeyboardInterrupt:
        logger.info("Shutdown requested.")
    finally:
        scheduler.stop()
        builtin.shutdown()
        # Clean up per-session nsjail temp dir
        try:
            shutil.rmtree(nsjail_session_tmpdir, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass
        if graph_memory_writer is not None:
            try:
                graph_memory_writer.stop()
            except Exception:  # noqa: BLE001
                pass
        if graph_memory_store is not None:
            try:
                graph_memory_store.close()
            except Exception:  # noqa: BLE001
                pass
        llm.close()
        if mcp_manager:
            mcp_manager.close_all()
            logger.info("MCP servers closed.")
        logger.info("Agent stopped.")


if __name__ == "__main__":
    main()

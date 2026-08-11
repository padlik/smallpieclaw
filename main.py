"""
main.py
-------
Entry point for the Telegram Home Server Agent.

Boot sequence:
  1. Parse --agent-name, resolve XDG paths, create XDG dirs, run migration check
  2. Load config
  3. Initialise all components
  4. Build the semantic tool index
  5. Start the scheduler
  6. Start the Telegram bot (blocking)
"""

from __future__ import annotations

import argparse
import fcntl
import logging
import os
import shutil
import signal
import sys
import tempfile
from pathlib import Path

import agent_logging

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
from config_schema import resolve_model_id, parse_vault_content  # noqa: E402
from graph_memory import create_graph_memory  # noqa: E402
from llm_client import LLMClient  # noqa: E402
from mcp_client import MCPManager  # noqa: E402
from memory_store import MemoryStore, WorkingMemory, ResultsMemory  # noqa: E402
from prompt_registry import PromptRegistry  # noqa: E402
from scheduler import Scheduler  # noqa: E402
from skill_registry import SkillRegistry  # noqa: E402
from strategy_memory import StrategyMemory  # noqa: E402
from telegram_interface import TelegramInterface  # noqa: E402
from token_usage import get_registry as get_token_registry  # noqa: E402
from tool_index import ToolIndex  # noqa: E402
from tool_registry import ToolRegistry  # noqa: E402
from conversation_io import _load_or_create_conversation_id, _save_conversation  # noqa: E402
import migrate  # noqa: E402
from xdg import XDGPaths, xdg_paths  # noqa: E402


def _create_xdg_dirs(paths: XDGPaths) -> None:
    """Create all XDG directories the agent needs. Idempotent."""
    for d in (paths.config_home, paths.data_home, paths.state_home, paths.cache_home,
              paths.logs_dir, paths.skills_dir, paths.mcp_tokens_dir):
        d.mkdir(parents=True, exist_ok=True)
    paths.runtime_dir.mkdir(parents=False, exist_ok=True)
    # state_home holds the vault, trust store, and conversation history — owner-only.
    try:
        os.chmod(paths.state_home, 0o700)
    except OSError:
        pass


def _warn_relative_paths(cfg: dict) -> None:
    """Warn on any string config value that looks like a relative path (starts with '.')."""
    def _scan(value, path: str) -> None:
        if isinstance(value, str):
            if value.startswith("."):
                logger.warning("Config value at %s looks like a relative path: %r", path, value)
        elif isinstance(value, dict):
            for k, v in value.items():
                _scan(v, f"{path}.{k}" if path else k)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                _scan(item, f"{path}[{i}]")

    _scan(cfg, "")


def _check_migration(paths: XDGPaths, agent_name: str) -> None:
    """Run the one-shot XDG migration if the old agent_home-relative layout is detected."""
    if migrate.migration_sentinel_exists(paths):
        return
    source = Path(__file__).parent
    if not (source / "config.toml").exists():
        return
    summary = migrate.main(agent_name, source)
    for line in summary:
        logger.info("migrate: %s", line)


def load_config(path: Path, vault_file: str | None = None):
    if not path.exists():
        logger.error("Config file not found: %s", path)
        sys.exit(f"No config found. Create: {path}")
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
        app_cfg = parse_config(cfg, vault_file=vault_file)
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        sys.exit(1)

    return app_cfg._raw, app_cfg


def _load_conversation(path: str, max_turns: int = 20):
    """Load ShortTermMemory from a conversation JSON file. Fresh on error."""
    from memory_store import ShortTermMemory as _ShortTermMemory

    if not os.path.exists(path):
        return _ShortTermMemory(max_turns=max_turns)
    try:
        import json as _json

        with open(path, "r", encoding="utf-8") as f:
            data = _json.load(f)
        return _ShortTermMemory.from_dict(data, max_turns=max_turns)
    except OSError:
        logger.warning(
            "Conversation file corrupted at %s — starting fresh", path, exc_info=True
        )
        return _ShortTermMemory(max_turns=max_turns)
    except ValueError:
        logger.warning(
            "Conversation file corrupted at %s — starting fresh", path, exc_info=True
        )
        return _ShortTermMemory(max_turns=max_turns)


def _cleanup_old_session_logs(state_dir: str, active_conv_id: str, retention_days: int) -> None:
    """Delete session_logs folders older than retention_days.

    Also deletes the corresponding conversation JSON file. The active
    conversation folder and its JSON file are always preserved.
    """
    import time as _time

    session_logs_root = os.path.join(state_dir, "session_logs")
    if not os.path.isdir(session_logs_root):
        return
    cutoff = _time.time() - (retention_days * 86400)
    conversations_dir = os.path.join(state_dir, "conversations")
    for entry in os.listdir(session_logs_root):
        conv_dir = os.path.join(session_logs_root, entry)
        if not os.path.isdir(conv_dir):
            continue
        if entry == active_conv_id:
            continue
        try:
            files = os.listdir(conv_dir)
            if not files:
                newest = os.path.getmtime(conv_dir)
            else:
                newest = max(
                    os.path.getmtime(os.path.join(conv_dir, f))
                    for f in files
                    if os.path.isfile(os.path.join(conv_dir, f))
                )
        except OSError:
            continue
        if newest < cutoff:
            try:
                shutil.rmtree(conv_dir, ignore_errors=True)
            except OSError:
                pass
            conv_file = os.path.join(conversations_dir, entry + ".json")
            if os.path.exists(conv_file):
                try:
                    _json_mtime = os.path.getmtime(conv_file)
                except OSError:
                    _json_mtime = 0
                if _json_mtime < cutoff:
                    try:
                        os.unlink(conv_file)
                    except OSError:
                        pass
            logger.info(
                "Cleaned up old session_logs for conversation %s (older than %d days)",
                entry,
                retention_days,
            )

    # Second pass: clean up orphaned conversation JSON files
    # (conversations that had no shell calls and thus no session_logs folder)
    if os.path.isdir(conversations_dir):
        for _jname in os.listdir(conversations_dir):
            if not _jname.endswith('.json'):
                continue
            _conv_id = _jname[:-5]
            if _conv_id == active_conv_id:
                continue
            # Skip if session_logs folder exists (already handled in first pass)
            if os.path.isdir(os.path.join(session_logs_root, _conv_id)):
                continue
            _jpath = os.path.join(conversations_dir, _jname)
            try:
                _jmtime = os.path.getmtime(_jpath)
            except OSError:
                continue
            if _jmtime < cutoff:
                try:
                    os.unlink(_jpath)
                except OSError:
                    pass
                logger.info(
                    "Cleaned up orphaned conversation JSON %s (older than %d days)",
                    _conv_id,
                    retention_days,
                )


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


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--agent-name", required=True,
        help="Agent name (required; resolves all XDG paths)",
    )
    return parser.parse_args()


def main():
    args = _parse_args()
    agent_name = args.agent_name
    paths = xdg_paths(agent_name)
    _check_migration(paths, agent_name)
    _create_xdg_dirs(paths)

    if not paths.config_file.exists():
        sys.exit(f"No config found. Create: {paths.config_file}")

    cfg, app_cfg = load_config(paths.config_file, vault_file=str(paths.secrets_file))
    _warn_relative_paths(cfg)

    workspace_dir = os.path.abspath(os.path.expanduser(
        cfg.get("paths", {}).get("workspace_dir", "~/Documents")
    ))
    downloads_dir = os.path.join(workspace_dir, "downloads")
    tmp_dir = f"/tmp/{agent_name}"

    # SIGTERM → clean exit so the finally: block always runs (e.g. systemctl stop)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    with _PidFileLock(str(paths.pid_file)):
        _run(
            cfg=cfg, app_cfg=app_cfg, paths=paths,
            downloads_dir=downloads_dir, tmp_dir=tmp_dir,
            workspace_dir=workspace_dir,
            agent_name=agent_name,
        )


def _run(
    cfg, app_cfg, paths: XDGPaths,
    downloads_dir, tmp_dir, workspace_dir,
    agent_name,
):
    """Core startup after PID lock is acquired."""
    # Re-initialise logging with the resolved XDG log paths and structlog dual sink.
    # Vault secret values are passed so they are redacted from every log record.
    json_log_path = agent_logging.setup_logging(
        str(paths.log_file),
        json_file=str(paths.log_jsonl),
        backup_count=int(cfg.get("paths", {}).get("log_backup_count", 30)),
        secret_values=_read_vault_secrets(str(paths.secrets_file)),
    )

    os.makedirs(downloads_dir, exist_ok=True)
    os.makedirs(tmp_dir, exist_ok=True)
    # nsjail_state_dir == paths.state_home — kept as a local alias since it's
    # referenced heavily below (trusted_dirs.json, conversations, session_logs).
    nsjail_state_dir = str(paths.state_home)
    conversation_id = _load_or_create_conversation_id(nsjail_state_dir)
    conversations_dir = os.path.join(nsjail_state_dir, "conversations")
    os.makedirs(conversations_dir, mode=0o700, exist_ok=True)
    try:
        os.chmod(conversations_dir, 0o700)
    except OSError:
        pass
    try:
        _cleanup_old_session_logs(
            nsjail_state_dir, conversation_id, app_cfg.agent.session_logs_retention_days
        )
    except OSError:
        logger.warning("session_logs retention cleanup failed", exc_info=True)
    trusted_dirs_path = os.path.join(nsjail_state_dir, "trusted_dirs.json")
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
    shell_nsjail_dump_config_on_error = bool(agent_cfg.get("shell_nsjail_dump_config_on_error", False))
    allow_net = app_cfg.agent.allow_net
    dns_nameserver = app_cfg.agent.dns_nameserver
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

    vault_file = str(paths.secrets_file)
    vault_secrets = _read_vault_secrets(vault_file)
    data_dir = str(paths.data_home)
    skills_dir_abs = str(paths.skills_dir)

    llm      = LLMClient(cfg, usage_registry=get_token_registry(), caller_tag="main")
    memory   = MemoryStore(str(paths.memory_file))
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
        shell_nsjail_dump_config_on_error=shell_nsjail_dump_config_on_error,
        allow_net=allow_net,
        nsjail_dns_nameserver=dns_nameserver,
        nsjail_session_tmpdir=nsjail_session_tmpdir,
        skills_dir=skills_dir_abs,
        nsjail_trusted_dirs_path=trusted_dirs_path,
        nsjail_agent_dir=str(Path(__file__).parent.resolve()),
        agent_name=agent_name,
        tmp_dir=tmp_dir,
        state_home=nsjail_state_dir,
        vault_secrets=vault_secrets,
    )
    builtin.conversation_id = conversation_id
    index    = ToolIndex(registry=registry, llm=llm, index_path=str(paths.tool_index_file), builtin_executor=builtin)

    # Initialise MCP servers (optional — skip if none configured)
    mcp_manager: MCPManager | None = None
    mcp_server_cfgs = cfg.get("mcp_servers", [])
    if mcp_server_cfgs:
        logger.info("Initialising %d MCP server(s)...", len(mcp_server_cfgs))
        mcp_manager = MCPManager(mcp_server_cfgs, mcp_tokens_dir=paths.mcp_tokens_dir)
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

    results_path  = str(paths.data_home / "results_memory.json")
    short_term    = _load_conversation(
        os.path.join(conversations_dir, conversation_id + ".json")
    )
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
        workspace_dir=workspace_dir,
        downloads_dir=downloads_dir,
        data_dir=data_dir,
        agent_name=agent_name,
        vault_path=vault_file,
        trusted_dirs_path=trusted_dirs_path,
        skills_dir=skills_dir_abs,
    )
    builtin.trusted_zone_checker = _trusted_zone_checker  # type: ignore[attr-defined]
    # NOTE: JSON LongTermMemory is no longer constructed or wired into runtime
    # agents (P2 consolidation). Runtime semantic recall is served by graph
    # memory; the legacy JSON store is migration/backfill-only via
    # backfill_graph_memory.py.

    skills = SkillRegistry(skills_dir=skills_dir_abs)
    logger.info("Loaded %d skill(s) from %s", skills.count(), skills_dir_abs)
    builtin.skill_registry = skills  # type: ignore[attr-defined]

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
        log_file=str(paths.log_file),
        log_backup_count=int(cfg.get("paths", {}).get("log_backup_count", 30)),
        creativity_mode=creativity_mode,
        plan_max_iterations=plan_max_iterations,
        inactivity_warn_minutes=inactivity_warn_minutes,
    )

    # Wire strategy memory into the agent so the ReAct loop can query learned
    # approaches in a later iteration. Assigned post-construction to avoid
    # changing the AgentController signature today.
    agent.strategy_memory = strategy_mem
    agent.trusted_zone_checker = _trusted_zone_checker  # type: ignore[attr-defined]
    # Let reset_task() save/rotate the conversation id.
    agent._conversation_state_dir = nsjail_state_dir  # type: ignore[attr-defined]

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
        paths=paths,
        scheduler_config_path=str(paths.scheduler_config),
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
                db_path=str(paths.graph_memory_db),
                embedder_fn=_embedder_fn,
                llm_call_fn=_llm_call_fn,
                embedding_dim=len(llm.embed("test")),
            )
            if graph_memory_store is not None:
                # Wire into agent (main react_loop context)
                agent._graph_memory = graph_memory_store  # type: ignore[attr-defined]
                agent._graph_memory_writer = graph_memory_writer  # type: ignore[attr-defined]
                agent._graph_memory_max_entries = app_cfg.graph_memory.max_context_entries
                # Wire into builtin executor for memory_graph_search/store tools
                builtin._graph_memory = graph_memory_store  # type: ignore[attr-defined]
                builtin._graph_memory_writer = graph_memory_writer  # type: ignore[attr-defined]
                logger.info(
                    "Graph memory enabled (db=%s, extraction_model=%s)",
                    paths.graph_memory_db,
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
    tg.agent = agent  # type: ignore[attr-defined]  # wire agent for confirm/resume and /models
    tg._graph_memory_store = graph_memory_store  # type: ignore[attr-defined]
    tg._graph_memory_writer = graph_memory_writer  # type: ignore[attr-defined]
    tg._prompt_registry = prompt_registry  # type: ignore[attr-defined]  # wire prompt registry for /prompts and lifecycle

    # Wire the Telegram interface into the MCP manager so OAuth redirect URLs
    # can be posted to the operator as inline buttons during the auth flow.
    if mcp_manager is not None:
        mcp_manager.set_tg_iface(tg)

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
        # Read the current conversation_id at shutdown — it may have been rotated
        # by /reset during the session, so we cannot use the startup variable.
        current_conv_id = getattr(builtin, "conversation_id", "") or conversation_id
        if current_conv_id:
            try:
                _save_conversation(
                    os.path.join(nsjail_state_dir, "conversations", current_conv_id + ".json"),
                    agent.short_term,
                )
            except Exception:  # noqa: BLE001
                logger.warning("Failed to save conversation on shutdown", exc_info=True)
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

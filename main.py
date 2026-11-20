"""
main.py
-------
Entry point for the Telegram Home Server Agent.

Boot sequence:
  1. Parse --agent-name, resolve XDG paths, create XDG dirs, run migration check
  2. Load config
  3. Refresh the provider model catalog, restore a valid remembered Main,
     and initialise all components
  4. Build the semantic tool index
  5. Start the scheduler
  6. Start the Telegram bot (blocking)
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import logging
import os
import shutil
import signal
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

import agent_logging

# PID file locking — prevents multiple concurrent instances. The shared lock
# lives in process_lock so offline maintenance tools exclude a
# live daemon; _PidFileLock is the compatibility name.
from process_lock import PidFileLock as _PidFileLock

# Bootstrap logging to stdout until config (and the resolved XDG log path) load.
agent_logging.setup_bootstrap()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Third-party & local imports
# ---------------------------------------------------------------------------
try:
    import tomli
except ImportError:
    import tomllib as tomli  # Python 3.11+

import httpx  # noqa: E402
from agent_controller import AgentController, RunClientBinding, SubAgentRunner  # noqa: E402
from agent_runtime import AgentRuntime, RuntimeOptions, RuntimeProfile  # noqa: E402
from builtin_executor import BuiltinExecutor  # noqa: E402
from builtin_tools.text_utils import redact_vault_secrets  # noqa: E402
from config_schema import (  # noqa: E402
    AgentConfig,
    AppConfig,
    ExecutorPaths,
    parse_vault_content,
    resolve_provider_connection,
)
from context_monitor import ContextMonitor  # noqa: E402
from embedding_client import EmbeddingClient  # noqa: E402
from exceptions import ConfigError  # noqa: E402
from graph_memory import (  # noqa: E402
    GraphMemoryStopError,
    GraphMemoryStore,
    GraphMemoryWriter,
    GraphPrerequisiteError,
    create_graph_memory,
    resolve_graph_startup,
)
from llm_client import LLMClient  # noqa: E402
from mcp_client import MCPManager  # noqa: E402
from model_discovery import ModelDiscovery  # noqa: E402
from model_registry import CatalogView, ModelRegistry  # noqa: E402
from model_settings import ModelCache, ModelPreference  # noqa: E402
from memory_store import MemoryStore, ShortTermMemory, WorkingMemory, ResultsMemory  # noqa: E402
from path_policy import PathPolicy  # noqa: E402
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


def load_config(path: Path, vault_file: str | None = None, agent_name: str = "piclaw"):
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
        app_cfg = parse_config(cfg, vault_file=vault_file, agent_name=agent_name)
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


def _bootstrap_dot_home(agent_name: str, xdg_state_home: str) -> tuple[str, str]:
    """Create the agent dot-home exchange surfaces (ADR-0028).

    Creates ``~/.<agent_name>/skills/`` and ``~/.<agent_name>/results/`` and
    performs the one-time idempotent skills migration: when the dot-home
    skills dir is empty and the legacy XDG state skills dir
    (``$XDG_STATE_HOME/<agent_name>/skills/``) exists and is non-empty, its
    contents are COPIED into the dot-home (the legacy dir is left untouched).
    Subsequent startups skip the copy while the target is populated.

    Returns:
        ``(skills_dir, results_dir)`` — absolute dot-home paths.
    """
    dot_home = os.path.join(os.path.expanduser("~"), f".{agent_name}")
    skills_dir = os.path.join(dot_home, "skills")
    results_dir = os.path.join(dot_home, "results")
    os.makedirs(skills_dir, exist_ok=True)
    os.makedirs(results_dir, exist_ok=True)

    if not os.listdir(skills_dir):
        legacy_skills = os.path.join(xdg_state_home, "skills")
        if os.path.isdir(legacy_skills) and os.listdir(legacy_skills):
            shutil.copytree(legacy_skills, skills_dir, dirs_exist_ok=True)
            logger.info(
                "Migrated legacy skills from %s to %s (one-time copy)",
                legacy_skills, skills_dir,
            )
    return skills_dir, results_dir


def _build_executor_paths(
    cfg: dict,
    paths: XDGPaths,
    data_dir: str,
    skills_dir: str,
    tmp_dir: str,
    downloads_dir: str,
    workspace_dir: str,
    nsjail_state_dir: str,
    nsjail_session_tmpdir: str,
    vault_file: str,
    vault_secrets: list[str],
    log_store_path: str,
    results_dir: str = "",
) -> ExecutorPaths:
    """Assemble the runtime filesystem/limit bundle for BuiltinExecutor.

    Bundles every resolved filesystem path, log retention setting, and vault
    secret list so the executor and controller receive a single typed object
    instead of ad-hoc strings.
    """
    return ExecutorPaths(
        tmp_dir=tmp_dir,
        downloads_dir=downloads_dir,
        workspace_dir=workspace_dir,
        log_file=str(paths.log_file),
        log_backup_count=int(cfg.get("paths", {}).get("log_backup_count", 30)),
        data_dir=data_dir,
        state_home=nsjail_state_dir,
        skills_dir=skills_dir,
        vault_path=vault_file,
        log_store_path=log_store_path,
        nsjail_session_tmpdir=nsjail_session_tmpdir,
        nsjail_agent_dir=str(Path(__file__).parent.resolve()),
        results_dir=results_dir,
        vault_secrets=vault_secrets,
    )


def _build_embedder(embedding: EmbeddingClient) -> Callable[[str], list[float]]:
    """Return a callable that embeds text using the root embedding client."""
    return embedding.embed


def _make_embedding_factory(
    app_cfg: AppConfig, vault_file: str,
) -> Callable[[threading.Event], EmbeddingClient]:
    """Return a factory building one runner-owned embedding client per event.

    Each call constructs a distinct ``EmbeddingClient`` from the explicit
    ``[embeddings]`` settings, bound to *event*; the caller owns and closes it.
    """

    def embedding_factory(event: threading.Event) -> EmbeddingClient:
        """Build an embedding client bound to *event* from explicit settings."""
        return EmbeddingClient(
            app_cfg.embeddings, vault_file=vault_file, cancel_event=event,
        )

    return embedding_factory


def _make_run_client_factory(
    model_registry: ModelRegistry,
    agent_cfg: AgentConfig,
    embedding: EmbeddingClient,
) -> Callable[[threading.Event], RunClientBinding]:
    """Return the main controller's per-run client factory.

    Each call admits the active Main exactly once, builds a run-owned
    ``LLMClient`` pinned to that admission (normal usage accounting, the shared
    root embedding client, the run's cancel event), and returns it with a view
    callback closing over the same immutable pin. Admission errors propagate
    before any client is allocated.
    """

    def run_client_factory(event: threading.Event) -> RunClientBinding:
        """Admit Main once and bind its run-owned client to the pinned view."""
        pin = model_registry.admit()
        client = LLMClient(
            pin,
            agent_config=agent_cfg,
            embedding_client=embedding,
            cancel_event=event,
            usage_registry=get_token_registry(),
            caller_tag="main",
        )

        def catalog_view() -> CatalogView:
            """Present the current catalog with this run's admitted pin."""
            return model_registry.view(pinned=pin)

        return RunClientBinding(client, catalog_view)

    return run_client_factory


def _make_sub_agent_factory(
    runtime: AgentRuntime, model_registry: ModelRegistry,
) -> Callable[..., SubAgentRunner]:
    """Return the sub-agent factory that admits one pin per call on its profile branch.

    Building the factory does no work. Each call admits exactly one ``RunModel``
    — ``admit_scheduled`` for ``SCHEDULED_AGENT`` (raw selector forwarded
    unchanged), ``admit`` for the ordinary profiles — and hands that pin to
    ``AgentRuntime.create`` via ``RuntimeOptions.run_model``. Admission errors
    propagate before any runner or client is allocated.
    """

    def sub_agent_factory(
        model: object = None,
        context_key: str | None = None,
        label: str = "on-demand",
        notify_fn: Callable[[str], None] | None = None,
        max_iterations: int | None = None,
        *,
        on_tool_trace: Callable[..., None] | None = None,
        cancel_event: threading.Event | None = None,
        trace_id: str | None = None,
        context_payload: dict | None = None,
        prompt_variant: str | None = None,
        runtime_profile: RuntimeProfile | None = None,
    ) -> SubAgentRunner:
        """Admit a pin for *runtime_profile* (default on-demand) and build its runner."""
        profile = runtime_profile or RuntimeProfile.ON_DEMAND_SUBAGENT
        if profile is RuntimeProfile.SCHEDULED_AGENT:
            run_model = model_registry.admit_scheduled(model)
        else:
            run_model = model_registry.admit(model)
        options = RuntimeOptions(
            run_model=run_model,
            max_iterations=max_iterations,
            context_key=context_key,
            context_payload=context_payload,
            prompt_variant=prompt_variant,
            trace_id=trace_id,
            cancel_event=cancel_event,
            label=label,
        )
        return runtime.create(
            profile, options, notify_fn=notify_fn, on_tool_trace=on_tool_trace,
        )

    return sub_agent_factory


def _build_memory_stack(
    app_cfg,
    paths: XDGPaths,
    llm: object,
    conversations_dir: str,
    conversation_id: str,
    data_dir: str,
) -> tuple[ShortTermMemory, WorkingMemory, ResultsMemory, StrategyMemory, PromptRegistry]:
    """Build the runtime memory layers for the main agent.

    Returns the short-term conversation memory, working memory, results memory,
    strategy memory, and prompt registry used by the controller and executor.
    """
    results_path = str(paths.data_home / "results_memory.json")
    short_term = _load_conversation(
        os.path.join(conversations_dir, conversation_id + ".json")
    )
    working = WorkingMemory()
    results_mem = ResultsMemory(path=results_path, llm=llm)
    strategy_mem = StrategyMemory(data_dir=data_dir)
    prompt_registry = PromptRegistry(data_dir=data_dir)
    return short_term, working, results_mem, strategy_mem, prompt_registry


def _build_graph_memory(
    app_cfg: AppConfig,
    paths: XDGPaths,
    agent: AgentController,
    builtin: BuiltinExecutor,
    vault_file: str,
    embedder_fn: Callable[[str], list[float]],
) -> tuple[GraphMemoryStore | None, GraphMemoryWriter | None]:
    """Initialise graph memory (opt-in) and wire it into agent + executor.

    Returns ``(None, None)`` when disabled, when prerequisites fail, or when
    store/writer construction fails. Telegram, indexing, and ordinary services
    remain available on prerequisite failures.
    """
    if not app_cfg.graph_memory.enabled:
        return None, None

    try:
        prepared = resolve_graph_startup(app_cfg, vault_file=vault_file)
    except GraphPrerequisiteError as exc:
        logger.warning(
            "Graph memory initialisation failed (continuing without it): %s",
            exc,
        )
        return None, None

    try:
        from graph_memory import build_extraction_llm_call as _build_extraction_llm_call

        llm_call_fn = _build_extraction_llm_call(
            prepared.pin, app_cfg.agent, caller_tag="graph-extract"
        )
        graph_memory_store, graph_memory_writer = create_graph_memory(
            cfg=app_cfg,
            db_path=str(paths.graph_memory_db),
            embedder_fn=embedder_fn,
            llm_call_fn=llm_call_fn,
            embedding_dim=len(embedder_fn("test")),
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
                prepared.pin.model.id,
            )
        return graph_memory_store, graph_memory_writer
    except Exception as _gm_init_exc:
        # Class name only: provider/SDK messages may embed endpoints or credentials.
        logger.warning(
            "Graph memory initialisation failed (continuing without it): %s",
            type(_gm_init_exc).__name__,
        )
        return None, None


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

    # The shared lock is held from the first config read through startup, so a
    # concurrent migration can never restore originals around a live config load.
    with _PidFileLock(str(paths.pid_file)):
        if not paths.config_file.exists():
            sys.exit(f"No config found. Create: {paths.config_file}")

        cfg, app_cfg = load_config(paths.config_file, vault_file=str(paths.secrets_file), agent_name=agent_name)
        _warn_relative_paths(cfg)

        workspace_dir = os.path.abspath(os.path.expanduser(
            cfg.get("paths", {}).get("workspace_dir", f"~/Documents/{agent_name}_workspace")
        ))
        downloads_dir = os.path.join(workspace_dir, "downloads")
        tmp_dir = f"/tmp/{agent_name}"

        # SIGTERM → clean exit so the finally: block always runs (e.g. systemctl stop)
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

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
    # The graph-memory component log is routed explicitly (see ADR-0023).
    _known_secrets = _read_vault_secrets(str(paths.secrets_file))
    log_store_path = agent_logging.setup_logging(
        str(paths.log_file),
        db_path=str(paths.log_store),
        graph_memory_log=str(paths.graph_memory_log),
        backup_count=int(cfg.get("paths", {}).get("log_backup_count", 30)),
        secret_values=_known_secrets,
    )
    # Saved scheduled selectors are redacted with the same startup secrets,
    # under the same >= 6-character needle policy as log ingestion.
    selector_redactor = functools.partial(
        redact_vault_secrets,
        secrets=tuple(s for s in _known_secrets if len(s) >= 6),
        placeholder="***REDACTED***",
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
    os.environ["TMPDIR"] = tmp_dir
    os.environ["TMP"] = tmp_dir
    os.environ["TEMP"] = tmp_dir
    logger.info("Downloads dir: %s | Tmp dir: %s", downloads_dir, tmp_dir)

    # Per-session temp dir for nsjail /tmp bind mount — persists across nsjail
    # invocations within a session, cleaned up at agent shutdown.
    nsjail_session_tmpdir = tempfile.mkdtemp(prefix="nsjail-tmp-")
    logger.info("nsjail session tmpdir: %s", nsjail_session_tmpdir)

    vault_file = str(paths.secrets_file)
    vault_secrets = _read_vault_secrets(vault_file)
    data_dir = str(paths.data_home)
    # Skills live in the agent dot-home (ADR-0028); the one-time migration
    # from the legacy XDG state skills dir happens inside the bootstrap helper.
    skills_dir_abs, results_dir_abs = _bootstrap_dot_home(agent_name, nsjail_state_dir)

    # Frozen three-tier path policy (ADR-0026) — constructed once at startup;
    # invalid [security] config fails startup (fail closed, no fallback).
    try:
        path_policy = PathPolicy.create(
            agent_name=agent_name,
            workspace_dir=workspace_dir,
            downloads_dir=downloads_dir,
            tmp_dir=tmp_dir,
            skills_dir=skills_dir_abs,
            results_dir=results_dir_abs,
            data_home=str(paths.data_home),
            state_home=str(paths.state_home),
            config_home=str(paths.config_home),
            vault_path=vault_file,
            config_path=str(paths.config_file),
            prohibited_dirs=app_cfg.security.prohibited_dirs,
            allowed_dirs=app_cfg.security.allowed_dirs,
        )
    except ConfigError as exc:
        logger.error("Invalid [security] configuration: %s", exc)
        sys.exit(1)

    # Separate step cap for scheduled/background agents (chat sessions use max_iterations)
    _raw_sched_max = app_cfg.agent.scheduled_max_iterations
    scheduled_max_iter = min(_raw_sched_max, 500) if _raw_sched_max > 0 else 500

    executor_paths = _build_executor_paths(
        cfg, paths, data_dir, skills_dir_abs, tmp_dir, downloads_dir, workspace_dir,
        nsjail_state_dir, nsjail_session_tmpdir,
        vault_file, vault_secrets, log_store_path,
        results_dir=results_dir_abs,
    )

    logger.info("Initialising components...")

    # Root-owned model services are closed on every exit of _run — normal,
    # exceptional, and setup failures after allocation: the one explicit root
    # embedding client and the discovery HTTP clients. One bounded exception:
    # when graph worker termination is unconfirmed (GraphMemoryStopError), the
    # root embedding client the worker may still use is left open; discovery
    # clients still close. There is no standing chat client; ordinary runs
    # build their own pinned client per admission.
    retain_graph_dependencies = False

    def close_root_embedding() -> None:
        """Close the root embedding unless a live graph worker may still use it."""
        if not retain_graph_dependencies:
            root_embedding.close()

    with contextlib.ExitStack() as root_resources:
        root_embedding = EmbeddingClient(
            app_cfg.embeddings, vault_file=vault_file,
        )
        root_resources.callback(close_root_embedding)
        if not root_embedding.available:
            logger.warning(
                "Embeddings unavailable (%s); tool search and results recall run degraded.",
                app_cfg.embeddings.unavailable_reason or root_embedding.unavailable_reason,
            )
        discovery_http = root_resources.enter_context(
            httpx.Client(follow_redirects=False, trust_env=False)
        )
        # One model catalog/selection owner (distinct from the MCP ToolRegistry).
        model_registry = ModelRegistry(
            app_cfg.providers,
            ModelCache(paths.models_cache_file),
            ModelDiscovery(discovery_http),
            functools.partial(resolve_provider_connection, vault_file=vault_file),
            selector_redactor=selector_redactor,
            preference=ModelPreference(paths.model_selection_file),
        )
        if model_registry.cache_notice is not None:
            logger.warning("%s Cache file: %s", model_registry.cache_notice, paths.models_cache_file)
        if model_registry.selection_notice is not None:
            logger.warning(
                "%s Selection file: %s", model_registry.selection_notice, paths.model_selection_file,
            )
        _refresh_model_catalog(model_registry, paths.models_cache_file)
        try:
            _run_with_model_services(
                cfg=cfg, app_cfg=app_cfg, paths=paths,
                downloads_dir=downloads_dir,
                nsjail_state_dir=nsjail_state_dir,
                nsjail_session_tmpdir=nsjail_session_tmpdir,
                conversation_id=conversation_id,
                conversations_dir=conversations_dir,
                data_dir=data_dir,
                skills_dir_abs=skills_dir_abs,
                vault_file=vault_file,
                path_policy=path_policy,
                executor_paths=executor_paths,
                scheduled_max_iter=scheduled_max_iter,
                selector_redactor=selector_redactor,
                model_registry=model_registry,
                root_embedding=root_embedding,
            )
        except GraphMemoryStopError:
            # Set before the ExitStack unwinds so close_root_embedding skips it.
            retain_graph_dependencies = True
            raise


def _refresh_model_catalog(model_registry: ModelRegistry, cache_file: Path) -> None:
    """Select the startup Main: the saved selection, else the first cached candidate.

    Discovery runs at most once, and only when nothing is known: no preference
    file (a corrupt, unreadable or unconfigured one counts as present) and zero
    cached candidates of configured connections. It is the same reusable
    refresh as ``/models`` (one logical pass, no retry); failed providers keep
    their last-good entries as stale. A saved selection that is invalid,
    unreadable or unconfigured leaves Main unset with no automatic choice
    (the registry already reported why). When no Main results, startup
    continues (commands stay available) but model work is refused until an
    explicit ``/models`` selection.
    """
    if model_registry.needs_first_start_discovery:
        report = model_registry.refresh()
        for provider_id, code in report.error_codes:
            if provider_id in report.stale_provider_ids:
                logger.warning(
                    "Model catalog refresh failed for provider %s (%s); keeping last-good entries as stale.",
                    provider_id, code,
                )
        if not report.persisted:
            logger.warning(
                "Model cache could not be saved to %s; continuing with this session's discovery. "
                "Selection and preference are unchanged; use /models to retry.",
                cache_file,
            )
    view = model_registry.restore_selection()
    if view.active_model_id is None:
        logger.warning(
            "No main model is active (%s); select one with /models before model work can run.",
            view.selection_status,
        )


def _run_with_model_services(
    *,
    cfg: dict,
    app_cfg: AppConfig,
    paths: XDGPaths,
    downloads_dir: str,
    nsjail_state_dir: str,
    nsjail_session_tmpdir: str,
    conversation_id: str,
    conversations_dir: str,
    data_dir: str,
    skills_dir_abs: str,
    vault_file: str,
    path_policy: PathPolicy,
    executor_paths: ExecutorPaths,
    scheduled_max_iter: int,
    selector_redactor: Callable[[str], str],
    model_registry: ModelRegistry,
    root_embedding: EmbeddingClient,
) -> None:
    """Compose and serve the agent on root-owned model services.

    Model-resource ownership: ``_run``'s root ``ExitStack`` owns and closes
    *model_registry*'s discovery clients and *root_embedding*, including on
    early setup failure, except that *root_embedding* stays open when shutdown
    raises ``GraphMemoryStopError`` (graph worker termination unconfirmed; the
    graph store and logging are then also left open here). That guarantee covers root model resources only;
    the other (legacy) setup resources constructed here keep their existing
    cleanup behavior and are not newly guaranteed by the root stack.
    """
    # Single shared ContextMonitor instance — injected into both BuiltinExecutor
    # (for the context_profile tool) and AgentController (for the ReAct loop
    # publish path and /context command). Both must reference the same instance.
    # See ADR-0022.
    context_monitor = ContextMonitor()

    memory   = MemoryStore(str(paths.memory_file))
    # Purge any model/LLM facts the agent may have written in past sessions.
    # These are always stale — authoritative model info is the live model catalog
    # (provider connections in config.toml, discovered candidates in the model
    # cache, the Main selection in the preference file) and is injected fresh
    # into every system prompt via prompt_builder.format_models().
    _purged = memory.purge_matching("model", "llm")
    if _purged:
        logger.info("Startup: purged %d stale model/provider key(s) from memory store", _purged)
    registry = ToolRegistry()
    builtin = BuiltinExecutor(
        agent_cfg=app_cfg.agent,
        paths=executor_paths,
        scheduler=None,
        memory=memory,
        context_monitor=context_monitor,
        path_policy=path_policy,
    )
    builtin.conversation_id = conversation_id
    index    = ToolIndex(
        registry=registry, llm=root_embedding,
        index_path=str(paths.tool_index_file), builtin_executor=builtin,
    )

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

    short_term, working, results_mem, strategy_mem, prompt_registry = _build_memory_stack(
        app_cfg, paths, root_embedding, conversations_dir, conversation_id, data_dir,
    )
    # Wire working memory and results memory into builtin executor so that
    # implicit spawn_agent context summaries include recent tool results.
    builtin._working = working
    builtin._results = results_mem
    builtin._prompt_registry = prompt_registry
    # NOTE: JSON LongTermMemory is no longer constructed or wired into runtime
    # agents (P2 consolidation). Runtime semantic recall is served by graph
    # memory; the legacy JSON store is migration/backfill-only via
    # backfill_graph_memory.py.

    skills = SkillRegistry(skills_dir=skills_dir_abs)
    logger.info("Loaded %d skill(s) from %s", skills.count(), skills_dir_abs)
    builtin.skill_registry = skills  # type: ignore[attr-defined]

    # No standing chat client: each run admits Main once through the factory,
    # which returns a run-owned client bound to that pin's catalog view.
    agent = AgentController(
        agent_cfg=app_cfg.agent,
        paths=executor_paths,
        llm=None,
        run_client_factory=_make_run_client_factory(model_registry, app_cfg.agent, root_embedding),
        model_catalog_view_provider=model_registry.view,
        tool_index=index,
        memory=memory,
        short_term=short_term,
        working=working,
        results=results_mem,
        builtin_executor=builtin,
        skill_registry=skills,
        mcp_manager=mcp_manager,
        context_monitor=context_monitor,
        step_notify_interval=app_cfg.agent.step_notify_interval,
    )

    # Checkpoint store for LLM error recovery.
    from checkpoint_store import CheckpointStore, startup_checkpoint_notice
    checkpoint_store = CheckpointStore(data_dir)
    agent.checkpoint_store = checkpoint_store

    # Wire LLM error handling config into the agent runtime.
    if hasattr(app_cfg, "llm_error_handling"):
        agent._checkpoint_enabled = app_cfg.llm_error_handling.checkpoint_enabled
        agent._retry_timeout_seconds = app_cfg.llm_error_handling.retry_timeout_seconds

    # Wire strategy memory into the agent so the ReAct loop can query learned
    # approaches in a later iteration. Assigned post-construction to avoid
    # changing the AgentController signature today.
    agent.strategy_memory = strategy_mem
    # Frozen three-tier path policy (ADR-0026) — shared by file tools and the
    # nsjail builder; constructed once at startup, never mutated at runtime.
    agent.path_policy = path_policy  # type: ignore[attr-defined]
    # Let reset_task() save/rotate the conversation id.
    agent._conversation_state_dir = nsjail_state_dir  # type: ignore[attr-defined]

    logger.info("Building semantic tool index...")
    try:
        index.build()
    except Exception as exc:
        logger.warning("Tool index build failed (check embeddings API config): %s", exc)

    def agent_handler(
        user_id, text, progress_cb, images=None, *, prompt_id=None, trace_id=None, resume_from=None,
        milestone_notify_fn=None,
    ):
        return agent.run(
            text,
            progress_callback=progress_cb,
            images=images or None,
            prompt_id=prompt_id,
            trace_id=trace_id,
            resume_from=resume_from,
            milestone_notify_fn=milestone_notify_fn,
            # Telegram root requests and /resume are the only runs an operator
            # can answer a Retry/Cancel card for (ADR-0048).
            interactive_llm_recovery=True,
        )

    # Build TelegramInterface first so notify() can reference it
    # (tg is created after scheduler/sub_agent_factory wiring, so we use nonlocal)
    tg: TelegramInterface | None = None

    def notify(msg):
        if tg is not None:
            tg.send_message_to_users(msg)

    def notify_html(html_msg):
        if tg is not None:
            tg.send_html_to_users(html_msg)

    # Construction boundary for all sub-agent products (ADR-0007). The runtime
    # holds the shared, run-independent collaborators; per-call construction
    # knobs (including the admitted pin) are passed through RuntimeOptions.
    # Construction is uniform across the sub-agent profiles — the visibility
    # source (on-demand/scheduled/plan-step/diagnostic) is assigned later by
    # register_run, not by the runtime. Each runner owns its embedding client.
    agent_runtime = AgentRuntime(
        agent_cfg=app_cfg.agent,
        embedding_factory=_make_embedding_factory(app_cfg, vault_file),
        paths=executor_paths,
        tool_index=index,
        base_memory=memory,
        builtin_executor=builtin,
        skill_registry=skills,
        mcp_manager=mcp_manager,
        results=results_mem,
        usage_registry=get_token_registry(),
        notify_fn=notify,
        data_dir=data_dir,
        scheduled_max_iterations=scheduled_max_iter,
    )

    # Wire the sole sub-agent factory into the builtin executor: one admission
    # per call on its profile branch (spawn_agent, scheduler, PlanExecutor).
    builtin._sub_agent_factory = _make_sub_agent_factory(agent_runtime, model_registry)
    builtin._notify_html_fn = notify_html

    scheduler = Scheduler(
        cfg, notify_fn=notify,
        paths=paths,
        scheduler_config_path=str(paths.scheduler_config),
        builtin_executor=builtin,
        selector_redactor=selector_redactor,
    )
    builtin.scheduler = scheduler  # wire scheduler into built-in tool
    # Scheduled job history is not operator context (isolate-job-execution,
    # design decision 2): it stays in job-owned history, not the operator prompt.

    # Initialise graph memory (opt-in) and wire it into agent + executor.
    # Graph extraction uses only its own explicit [graph_memory] route (ADR-0042);
    # it consults neither the model catalog nor Main.
    _embedder_fn = _build_embedder(root_embedding)
    graph_memory_store, graph_memory_writer = _build_graph_memory(
        app_cfg, paths, agent, builtin, vault_file, _embedder_fn,
    )

    logger.info("Starting Telegram bot...")
    tg = TelegramInterface(
        cfg, agent_handler,
        agent_reset_fn=agent.reset_task,
        agent_compress_fn=agent.compress_context,
        scheduler=scheduler,
        tool_registry=registry,
        tool_index=index,
        skill_registry=skills,
        usage_registry=get_token_registry(),
        downloads_dir=downloads_dir,
        mcp_manager=mcp_manager,
        model_registry=model_registry,
    )
    tg.agent = agent  # type: ignore[attr-defined]  # wire agent for confirm/resume and /models
    tg._graph_memory_store = graph_memory_store  # type: ignore[attr-defined]
    tg._graph_memory_writer = graph_memory_writer  # type: ignore[attr-defined]
    tg._prompt_registry = prompt_registry  # type: ignore[attr-defined]  # wire prompt registry for /prompts and lifecycle

    # Startup checkpoint scan — notify operator of recoverable runs.
    try:
        startup_notice = startup_checkpoint_notice(checkpoint_store)
        if startup_notice is not None:
            notify(startup_notice)
    except Exception as exc:
        logger.warning("Startup checkpoint scan failed: %s", exc)

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
            # Worker termination gates every dependent close below. When it is
            # not confirmed, report the fixed safe message and propagate before
            # closing the store, stopping logging or reporting success.
            try:
                graph_memory_writer.stop()
            except GraphMemoryStopError as exc:
                logger.error(str(exc))
                raise
            except Exception as exc:
                logger.error(GraphMemoryStopError.FAILED_MESSAGE)
                raise GraphMemoryStopError(GraphMemoryStopError.FAILED_MESSAGE) from exc
        if graph_memory_store is not None:
            try:
                graph_memory_store.close()
            except Exception:  # noqa: BLE001
                pass
        if mcp_manager:
            mcp_manager.close_all()
            logger.info("MCP servers closed.")
        logger.info("Agent stopped.")
        agent_logging.shutdown_log_store()


if __name__ == "__main__":
    main()

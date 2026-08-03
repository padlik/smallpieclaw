"""
xdg.py
------
Central XDG Base Directory path resolution for agent storage.

``xdg_paths(agent_name)`` is the single source of truth for every path the
agent reads from or writes to. It is pure and side-effect free — it never
creates directories or files. Directory creation happens exclusively in
``main.py`` (``_create_xdg_dirs``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class XDGPaths:
    """Resolved XDG Base Directory paths for one agent instance."""

    # Roots (one per XDG bucket)
    config_home: Path
    data_home: Path
    state_home: Path
    cache_home: Path
    runtime_dir: Path

    # Derived leaf paths
    config_file: Path
    scheduler_config: Path
    memory_file: Path
    graph_memory_db: Path
    tool_index_file: Path
    pid_file: Path
    secrets_file: Path
    logs_dir: Path
    log_file: Path
    log_jsonl: Path
    skills_dir: Path
    scheduler_state: Path
    scheduler_commands: Path
    scheduler_jobs: Path
    job_execution_log: Path


def xdg_paths(agent_name: str) -> XDGPaths:
    """Resolve all XDG paths for *agent_name*.

    Reads XDG env vars with spec-compliant fallbacks. Pure and side-effect
    free — never creates directories.
    """
    xdg_config = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    xdg_data = Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser()
    xdg_state = Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser()
    xdg_cache = Path(os.environ.get("XDG_CACHE_HOME", "~/.cache")).expanduser()
    runtime_env = os.environ.get("XDG_RUNTIME_DIR", "")

    config_home = xdg_config / agent_name
    data_home = xdg_data / agent_name
    state_home = xdg_state / agent_name
    cache_home = xdg_cache / agent_name
    # $XDG_RUNTIME_DIR is always absolute when set by systemd-logind; no
    # expanduser needed. Falls back to state_home when unset.
    runtime_dir = Path(runtime_env) / agent_name if runtime_env else state_home

    logs_dir = state_home / "logs"

    return XDGPaths(
        config_home=config_home,
        data_home=data_home,
        state_home=state_home,
        cache_home=cache_home,
        runtime_dir=runtime_dir,
        config_file=config_home / "config.toml",
        scheduler_config=config_home / "scheduler.toml",
        memory_file=data_home / "memory.json",
        graph_memory_db=data_home / "graph_memory",
        tool_index_file=cache_home / "tool_index.json",
        pid_file=runtime_dir / "agent.pid",
        secrets_file=state_home / "secrets.toml",
        logs_dir=logs_dir,
        log_file=logs_dir / "agent.log",
        log_jsonl=logs_dir / "agent.jsonl",
        skills_dir=state_home / "skills",
        scheduler_state=state_home / "scheduler_state.json",
        scheduler_commands=state_home / "scheduler_commands.json",
        scheduler_jobs=state_home / "scheduler_jobs.json",
        job_execution_log=state_home / "job_execution_log.jsonl",
    )


def migration_sentinel_exists(paths: XDGPaths) -> bool:
    """Return True if a migration sentinel file exists in ``state_home``."""
    return any(paths.state_home.glob("migrated_from_*.sentinel"))


def write_migration_sentinel(paths: XDGPaths) -> None:
    """Write a new timestamped UTC sentinel file into ``state_home``."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (paths.state_home / f"migrated_from_{ts}.sentinel").write_text("")

# Use XDG Base Directory Specification for all agent storage paths

## Status

Accepted

## Date

2026-08-03

## Supersedes

None

## Context and Problem Statement

Agent storage (config, data, state, cache, runtime) was historically stored relative to `_AGENT_DIR` — the directory containing `main.py` — under a derived `agent_home` path. Config files, `data/`, and `skills/` all lived alongside source code. This entangled code with user-specific data: a `git pull` or `venv` rebuild could disturb memories, configuration, and scheduler state. Nine `[paths]` config parameters (`data_dir`, `memory_file`, `pid_file`, `log_file`, `file_vault`, `skills_dir`, `downloads_dir`, `tool_index_file`, `graph_memory.db_path`) allowed per-file overrides, fragmenting path resolution logic across modules and providing no coherent storage model.

ADR-0016 began retiring `_AGENT_DIR` as a storage root and ADR-0015 assumed XDG state paths for nsjail config. The natural completion of that direction is to adopt XDG Base Directory Specification as the universal layout: config (read rarely, written by humans) to `$XDG_CONFIG_HOME`; persistent data to `$XDG_DATA_HOME`; runtime state to `$XDG_STATE_HOME`; regeneratable cache to `$XDG_CACHE_HOME`; volatile runtime to `$XDG_RUNTIME_DIR`. Keying all paths by `agent_name` (not by code directory) allows multiple agent instances on one host and allows the code directory to be overwritten freely.

## Considered Options

- **Keep `agent_home`-relative storage with per-path config overrides** — Continue with `_AGENT_DIR` as the path root. Rejected: entangles code with data; every upgrade is potentially destructive; nine config parameters to manage with no coherent model.
- **Single XDG bucket (everything under `$XDG_STATE_HOME`)** — Simpler but semantically incorrect: config, cache, and runtime-volatile files are not state. Rejected: violates XDG semantics; undermines backup and inspection tooling.
- **Layered config lookup with cwd fallback** — Allow `agent_home` as a fallback for agent_name/config resolution. Rejected: increases complexity with no benefit; clean break is preferred.
- **Strict XDG split with `XDGPaths` dataclass** — A single pure resolver produces all paths from `agent_name` + env vars; callers receive a frozen dataclass. No per-path overrides in config.

## Decision Outcome

Chosen option: Strict XDG split with `XDGPaths` dataclass.

All agent storage paths are keyed by `agent_name` under XDG Base Directory buckets, resolved exclusively by `xdg_paths(agent_name: str) → XDGPaths` in `xdg.py`.

### Architectural commitments

1. **`xdg.py` is the single path resolver.** `XDGPaths` (frozen dataclass) is the single source of truth for all agent file/dir paths. No other module computes agent file locations. `xdg_paths()` is pure and side-effect-free; directory creation happens exclusively in `main.py`.

2. **`--agent-name <name>` is the required CLI arg and sole bootstrap identity.** No `agent_home`, no cwd fallback, no `[agent].agent_name` config field for path derivation. Omitting `--agent-name` is a startup error.

3. **`[paths]` retains only `workspace_dir`.** All other former `[paths]` fields (`data_dir`, `tool_index_file`, `memory_file`, `pid_file`, `downloads_dir`, `log_file`, `file_vault`, `skills_dir`) and `GraphMemoryConfig.db_path` are removed permanently. Future storage additions must use an XDG bucket, not a new `[paths]` override. `downloads_dir` continues to exist as a runtime value derived at startup as `<workspace_dir>/downloads/`; it is not an XDG-bucket path and does not appear in `XDGPaths`. `downloads_dir` continues to exist as a runtime value derived at startup as `<workspace_dir>/downloads/`; it is not an XDG-bucket path and does not appear in `XDGPaths`.

4. **`SPC_VAULT_FILE` and `SPC_LOG_DIR` env var overrides are retired.** Vault is always `paths.secrets_file`; logs are always `paths.logs_dir`. Per-path env var overrides for agent storage are not reintroduced.

5. **`agent_home` as a storage root concept is retired.** `agent_home` may continue to describe the code/venv directory in systemd `WorkingDirectory`, but it has no semantic role in path resolution.

### XDG bucket assignments

| Bucket | Paths |
|--------|-------|
| `$XDG_CONFIG_HOME/<name>/` | `config.toml`, `scheduler.toml` |
| `$XDG_DATA_HOME/<name>/` | `memory.json`, `graph_memory` (DB base) |
| `$XDG_CACHE_HOME/<name>/` | `tool_index.json` |
| `$XDG_STATE_HOME/<name>/` | `secrets.toml`, `logs/`, `skills/`, `scheduler_state.json`, `scheduler_commands.json`, `scheduler_jobs.json`, `job_execution_log.jsonl` |
| `$XDG_RUNTIME_DIR/<name>/` | `agent.pid` (fallback: `$XDG_STATE_HOME/<name>/agent.pid` if `$XDG_RUNTIME_DIR` unset) |

### Consequences

- **Positive**: The code directory can be overwritten by `git pull` or `venv` rebuild without touching any agent data. Code and data lifecycles are fully decoupled.
- **Positive**: All path resolution is concentrated in one pure function. No per-path override logic to audit, test, or document.
- **Positive**: XDG semantics are respected: cache is excluded from backups (`$XDG_CACHE_HOME`), runtime pid is volatile (`$XDG_RUNTIME_DIR`), config is managed by humans (`$XDG_CONFIG_HOME`).
- **Positive**: Multiple agent instances on one host are first-class: each `--agent-name` gets its own XDG subdirectory with no collision.
- **Positive**: Container and `$HOME`-remapped deployments work via standard `XDG_*` env var overrides; no agent-specific config needed.
- **Negative**: Breaking change for all existing installs. A `migrate.py` one-shot migration script with auto-trigger on first `--agent-name` run mitigates the upgrade path, but operators must update their launch commands.
- **Negative**: Operators who relied on per-path config overrides (e.g., custom `data_dir` on a separate volume) must reconfigure by setting `XDG_*` env vars instead.
- **Negative**: `skills_dir` is no longer user-configurable in `config.toml`; operators who placed skills under a non-default path must move them to `$XDG_STATE_HOME/<name>/skills/`.
- **Negative**: Deployments where `$XDG_RUNTIME_DIR` is set but its parent (`/run/user/<uid>`) does not exist (bare containers, WSL 1, non-systemd-logind hosts) will see a hard `FileNotFoundError` at startup because `_create_xdg_dirs()` uses `parents=False` for `runtime_dir`. Operators on such systems must either ensure the parent directory exists (systemd-logind creates it automatically for user sessions) or leave `XDG_RUNTIME_DIR` unset to fall back to the `$XDG_STATE_HOME/<name>/agent.pid` path.
- **Negative**: Deployments where `$XDG_RUNTIME_DIR` is set but its parent directory does not exist (bare containers, WSL 1, non-systemd-logind hosts) will fail at startup with `FileNotFoundError` because `_create_xdg_dirs()` uses `parents=False` for `runtime_dir`. Operators on such hosts must either ensure `$XDG_RUNTIME_DIR`'s parent exists before launch, or leave `$XDG_RUNTIME_DIR` unset to trigger the `$XDG_STATE_HOME` fallback for `agent.pid`.

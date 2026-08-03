## Why

Agent configuration, data, and runtime state are currently stored relative to the agent project directory (`agent_home`), entangling code with user-specific files. This prevents clean upgrades: updating the agent (git pull, venv rebuild) risks disturbing memories, configurations, and scheduler state. Moving all storage to XDG Base Directory paths separates code from data completely, so `agent_home` can be overwritten freely.

## What Changes

- **BREAKING** Remove `data_dir` config parameter — data dir is always `$XDG_DATA_HOME/<agent_name>/`
- **BREAKING** Remove `tool_index_file` config parameter — always `$XDG_CACHE_HOME/<agent_name>/tool_index.json` (regeneratable; never migrated)
- **BREAKING** Remove `memory_file` config parameter — always `$XDG_DATA_HOME/<agent_name>/memory.json`
- **BREAKING** Remove `pid_file` config parameter — always `$XDG_RUNTIME_DIR/<agent_name>/agent.pid` (fallback: `$XDG_STATE_HOME/<agent_name>/agent.pid` if `$XDG_RUNTIME_DIR` is not set)
- **BREAKING** Remove `downloads_dir` config parameter — always `<workspace_dir>/downloads/`; `workspace_dir` remains a `[paths]` config parameter (default: `~/Documents`)
- **BREAKING** Remove `log_file` config parameter — always `$XDG_STATE_HOME/<agent_name>/logs/agent.log` and `agent.jsonl` (no change for existing installs; already XDG)
- **BREAKING** Remove `file_vault` config parameter — always `$XDG_STATE_HOME/<agent_name>/secrets.toml` (no change for existing installs; already XDG)
- **BREAKING** Remove `graph_memory.db_path` config parameter — always `$XDG_DATA_HOME/<agent_name>/graph_memory`
- **BREAKING** Remove `skills_dir` config parameter — always `$XDG_STATE_HOME/<agent_name>/skills/`; skills are user-created only, not bundled with code
- **BREAKING** Move `config.toml` from `agent_home/` to `$XDG_CONFIG_HOME/<agent_name>/config.toml`
- **BREAKING** Move `scheduler.toml` from `agent_home/` to `$XDG_CONFIG_HOME/<agent_name>/scheduler.toml`
- **BREAKING** Move scheduler runtime state files from `data/` to `$XDG_STATE_HOME/<agent_name>/`: `scheduler_state.json`, `scheduler_commands.json`, `scheduler_jobs.json`, `job_execution_log.jsonl`
- **BREAKING** Change agent launch: `python main.py --agent-name <name>` replaces running from the agent directory; `_AGENT_DIR` concept retired; `agent_home` no longer serves as a storage root; `AgentConfig.agent_home` field removed from `[agent]` config section
- Add `xdg.py` module — central XDG path resolution, produces `XDGPaths` dataclass consumed by all modules
- Add `migrate.py` script — one-shot migration; CLI: `migrate.py --agent-name <name> [--source <agent_home_dir>]`; runs automatically on first `--agent-name` launch if old layout detected; can also be run manually; writes migration sentinel to prevent re-runs
- On startup: create all XDG directories (idempotent); error with clear message if config is missing; warn if config contains relative paths

## Non-Goals

- Layered config lookup (cwd fallback) — rejected; `--agent-name` is the only resolution path
- Bundled/default skills shipped with code — skills are user-created only
- Single-bucket layout (everything under `~/.local/state`) — strict per-type XDG split is used
- Retaining `_AGENT_DIR` as a reference point for path resolution

## Capabilities

### New Capabilities

- `xdg-path-resolution`: All agent storage paths derived from XDG env vars and `--agent-name`; no per-path config parameters; `XDGPaths` dataclass passed to all consumers
- `agent-xdg-launch`: Agent launched with `--agent-name <name>` flag from any working directory; config resolved from `$XDG_CONFIG_HOME/<name>/config.toml`; XDG dirs created at startup; missing config produces actionable error
- `xdg-data-migration`: `migrate.py --agent-name <name> [--source <agent_home_dir>]` detects old `agent_home`-relative layout and copies files to correct XDG locations; writes migration sentinel to prevent re-runs; skips and removes regeneratable files (`tool_index.json`)

### Modified Capabilities

- `skill-path-resolution`: Skills directory changes from project-relative (`agent_home/skills/`) to user-global XDG state (`$XDG_STATE_HOME/<name>/skills/`); skills are user-created only, no bundled skills shipped with code
- `skills-dir-sandbox-mount`: nsjail read-only whitelist for skills dir updated from project-relative path to `$XDG_STATE_HOME/<name>/skills/`; `skills_dir` config parameter in `[paths]` removed; path is always XDG-derived
- `agent-scoped-directories`: `agent_home` no longer serves as a storage root — it is the code directory only (WorkingDirectory in systemd); `AgentConfig.agent_home` field removed; all storage paths derive from XDG + `agent_name`; `log_file` override scenario removed (parameter no longer exists)

## Impact

- `main.py` — major refactor: `_AGENT_DIR` retired, new startup flow, XDG dir creation, auto-migration check, `--agent-name` CLI arg
- `xdg.py` — new module
- `migrate.py` — new script
- `config_schema.py` — remove 8 `PathsConfig` fields (`data_dir`, `tool_index_file`, `memory_file`, `pid_file`, `downloads_dir`, `log_file`, `file_vault`, `skills_dir`), remove `GraphMemoryConfig.db_path`, remove `AgentConfig.agent_home`; XDG path helpers updated
- `scheduler.py` — state/commands/jobs/log paths derived from `XDGPaths`
- `nsjail_config.py` — skills path whitelist updated
- `tests/conftest.py` — `tmp_agent_dir` fixture replaced by `tmp_xdg` (overrides all `XDG_*` env vars to temp dirs)
- Systemd user service units: add `--agent-name <name>` to `ExecStart`; `$XDG_RUNTIME_DIR` already set by systemd-logind

# XDG Path Resolution Specification

## Purpose

Define the `XDGPaths` frozen dataclass and `xdg_paths(agent_name)` resolver in the new `xdg.py` module.

## Requirements

### Requirement: XDG Base Directory resolution

The `xdg_paths(agent_name)` function MUST resolve all agent paths from XDG Base Directory environment variables with XDG-spec-defined fallbacks. It MUST be pure and side-effect free — it never creates directories or writes any files.

Feature: XDG path resolution
Rule: `xdg_paths(agent_name)` reads XDG env vars with standard defaults, appends `agent_name` to each base, and returns a frozen `XDGPaths` dataclass; no directories are created as a side effect.

#### Scenario: Default XDG paths with no env vars set
- **GIVEN** none of `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME`, `XDG_CACHE_HOME`, or `XDG_RUNTIME_DIR` are set in the environment
- **WHEN** `xdg_paths("piclaw")` is called
- **THEN** `config_home` is `~/.config/piclaw`
- **AND** `data_home` is `~/.local/share/piclaw`
- **AND** `state_home` is `~/.local/state/piclaw`
- **AND** `cache_home` is `~/.cache/piclaw`
- **AND** `runtime_dir` is `~/.local/state/piclaw` (fallback: `state_home / agent_name` when `XDG_RUNTIME_DIR` is unset)
- **AND** `config_file` is `~/.config/piclaw/config.toml`
- **AND** `scheduler_config` is `~/.config/piclaw/scheduler.toml`
- **AND** `memory_file` is `~/.local/share/piclaw/memory.json`
- **AND** `graph_memory_db` is `~/.local/share/piclaw/graph_memory`
- **AND** `tool_index_file` is `~/.cache/piclaw/tool_index.json`
- **AND** `pid_file` is `~/.local/state/piclaw/agent.pid`
- **AND** `logs_dir` is `~/.local/state/piclaw/logs`
- **AND** `log_file` is `~/.local/state/piclaw/logs/agent.log`
- **AND** `log_jsonl` is `~/.local/state/piclaw/logs/agent.jsonl`
- **AND** `skills_dir` is `~/.local/state/piclaw/skills`
- **AND** `secrets_file` is `~/.local/state/piclaw/secrets.toml`
- **AND** `scheduler_state` is `~/.local/state/piclaw/scheduler_state.json`
- **AND** `scheduler_commands` is `~/.local/state/piclaw/scheduler_commands.json`
- **AND** `scheduler_jobs` is `~/.local/state/piclaw/scheduler_jobs.json`
- **AND** `job_execution_log` is `~/.local/state/piclaw/job_execution_log.jsonl`

#### Scenario: Custom XDG_CONFIG_HOME overrides config root
- **GIVEN** `XDG_CONFIG_HOME` is set to `/custom/cfg`
- **AND** all other XDG env vars are unset
- **WHEN** `xdg_paths("piclaw")` is called
- **THEN** `config_home` is `/custom/cfg/piclaw`
- **AND** `config_file` is `/custom/cfg/piclaw/config.toml`
- **AND** `scheduler_config` is `/custom/cfg/piclaw/scheduler.toml`
- **AND** `data_home`, `state_home`, `cache_home`, and `runtime_dir` use their standard XDG defaults

#### Scenario: XDG_RUNTIME_DIR set — runtime_dir includes agent name as subdirectory
- **GIVEN** `XDG_RUNTIME_DIR` is set to `/run/user/1000`
- **AND** all other XDG env vars are unset
- **WHEN** `xdg_paths("piclaw")` is called
- **THEN** `runtime_dir` is `/run/user/1000/piclaw`
- **AND** `pid_file` is `/run/user/1000/piclaw/agent.pid`
- **AND** `runtime_dir` is NOT simply `/run/user/1000`

#### Scenario: XDG_RUNTIME_DIR unset — runtime_dir falls back to state_home
- **GIVEN** `XDG_RUNTIME_DIR` is not set
- **AND** `XDG_STATE_HOME` is not set
- **WHEN** `xdg_paths("piclaw")` is called
- **THEN** `runtime_dir` is `~/.local/state/piclaw`
- **AND** `runtime_dir` equals `state_home`
- **AND** `pid_file` is `~/.local/state/piclaw/agent.pid`

#### Scenario: xdg_paths() is pure — no directories created
- **GIVEN** none of the XDG base directories exist on the filesystem
- **WHEN** `xdg_paths("piclaw")` is called
- **THEN** the call succeeds and returns a valid `XDGPaths` instance
- **AND** no directories are created on the filesystem
- **AND** no files are written
- **AND** the filesystem state is identical before and after the call

### Requirement: Migration sentinel helpers

The `xdg.py` module MUST provide `migration_sentinel_exists(paths)` and `write_migration_sentinel(paths)` to detect and record completed one-shot migrations.

Feature: Migration sentinel
Rule: `write_migration_sentinel` writes a timestamped `migrated_from_<ts>.sentinel` file in `state_home`; `migration_sentinel_exists` returns `True` if any such file is present.

#### Scenario: migration_sentinel_exists returns True after write_migration_sentinel
- **GIVEN** `paths.state_home` exists on the filesystem
- **AND** `migration_sentinel_exists(paths)` returns `False` (no sentinel file present)
- **WHEN** `write_migration_sentinel(paths)` is called
- **THEN** `migration_sentinel_exists(paths)` returns `True`
- **AND** exactly one file matching the glob `migrated_from_*.sentinel` exists in `paths.state_home`

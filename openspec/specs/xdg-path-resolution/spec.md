# XDG Path Resolution Specification

## Purpose

Define the `XDGPaths` frozen dataclass and `xdg_paths(agent_name)` resolver in the new `xdg.py` module.

## Requirements
### Requirement: XDG Base Directory resolution

`xdg_paths(agent_name)` MUST resolve all agent paths from XDG environment variables and standard fallbacks, purely and without side effects, returning a frozen `XDGPaths` and creating no directories or files. It SHALL expose `model_selection_file` below `config_home` and `models_cache_file` below `cache_home`; callers SHALL NOT reconstruct these paths locally. It SHALL NOT expose any path for the retired combined settings file.

#### Scenario: Default XDG paths with no env vars set
- **GIVEN** none of `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME`, `XDG_CACHE_HOME`, or `XDG_RUNTIME_DIR` are set
- **WHEN** `xdg_paths("piclaw")` is called
- **THEN** `config_home` is `~/.config/piclaw`
- **AND** `data_home` is `~/.local/share/piclaw`
- **AND** `state_home` is `~/.local/state/piclaw`
- **AND** `cache_home` is `~/.cache/piclaw`
- **AND** `runtime_dir` is `~/.local/state/piclaw`
- **AND** `config_file` is `~/.config/piclaw/config.toml`
- **AND** `scheduler_config` is `~/.config/piclaw/scheduler.toml`
- **AND** `model_selection_file` is `~/.config/piclaw/model-selection.toml`
- **AND** `models_cache_file` is `~/.cache/piclaw/models-cache.json`
- **AND** `memory_file` is `~/.local/share/piclaw/memory.json`
- **AND** `graph_memory_db` is `~/.local/share/piclaw/graph_memory`
- **AND** `tool_index_file` is `~/.cache/piclaw/tool_index.json`
- **AND** `pid_file` is `~/.local/state/piclaw/agent.pid`
- **AND** `logs_dir` is `~/.local/state/piclaw/logs`
- **AND** `log_file` is `~/.local/state/piclaw/logs/agent.log`
- **AND** `log_store` is `~/.local/state/piclaw/logs/agent_logs.sqlite`
- **AND** `skills_dir` is `~/.local/state/piclaw/skills`
- **AND** `secrets_file` is `~/.local/state/piclaw/secrets.toml`
- **AND** `scheduler_state` is `~/.local/state/piclaw/scheduler_state.json`
- **AND** `scheduler_commands` is `~/.local/state/piclaw/scheduler_commands.json`
- **AND** `scheduler_jobs` is `~/.local/state/piclaw/scheduler_jobs.json`
- **AND** `job_execution_log` is `~/.local/state/piclaw/job_execution_log.jsonl`

#### Scenario: Custom XDG_CONFIG_HOME overrides config root
- **GIVEN** `XDG_CONFIG_HOME=/custom/cfg` and other XDG variables are unset
- **WHEN** `xdg_paths("piclaw")` is called
- **THEN** `config_home` is `/custom/cfg/piclaw`
- **AND** `config_file` is `/custom/cfg/piclaw/config.toml`
- **AND** `scheduler_config` is `/custom/cfg/piclaw/scheduler.toml`
- **AND** `model_selection_file` is `/custom/cfg/piclaw/model-selection.toml`
- **AND** data, state, cache, and runtime paths use their standard defaults

#### Scenario: Custom XDG_CACHE_HOME moves only the cache
- **GIVEN** `XDG_CACHE_HOME=/custom/cache` and other XDG variables are unset
- **WHEN** `xdg_paths("piclaw")` is called
- **THEN** `models_cache_file` is `/custom/cache/piclaw/models-cache.json`
- **AND** `model_selection_file` remains `~/.config/piclaw/model-selection.toml`

#### Scenario: XDG_RUNTIME_DIR set — runtime_dir includes agent name as subdirectory
- **GIVEN** `XDG_RUNTIME_DIR=/run/user/1000` and other XDG variables are unset
- **WHEN** `xdg_paths("piclaw")` is called
- **THEN** `runtime_dir` is `/run/user/1000/piclaw`
- **AND** `pid_file` is `/run/user/1000/piclaw/agent.pid`
- **AND** `runtime_dir` is not simply `/run/user/1000`

#### Scenario: XDG_RUNTIME_DIR unset — runtime_dir falls back to state_home
- **GIVEN** `XDG_RUNTIME_DIR` and `XDG_STATE_HOME` are unset
- **WHEN** `xdg_paths("piclaw")` is called
- **THEN** `runtime_dir` is `~/.local/state/piclaw`
- **AND** it equals `state_home`
- **AND** `pid_file` is `~/.local/state/piclaw/agent.pid`

#### Scenario: xdg_paths() is pure — no directories created
- **GIVEN** no XDG base directories exist
- **WHEN** `xdg_paths("piclaw")` is called
- **THEN** a valid `XDGPaths` is returned without filesystem changes
- **AND** no directories are created or files written

#### Scenario: Model settings remain in protected XDG config home
- **GIVEN** `model_selection_file` has been resolved
- **WHEN** application components access the selection preference
- **THEN** it remains under agent `config_home`
- **AND** file tools and the jail do not expose or mount it

#### Scenario: Cache holds no secrets in its unprotected location
- **GIVEN** `models_cache_file` has been resolved below `cache_home`
- **WHEN** the cache is written
- **THEN** it contains no credential, credential reference, or endpoint URL
- **AND** existing path-policy and jail rules are not changed for it

#### Scenario: Retired combined settings path is not exposed
- **GIVEN** `xdg_paths("piclaw")` is called
- **WHEN** the returned `XDGPaths` is inspected
- **THEN** it has no `models_config_file` attribute

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

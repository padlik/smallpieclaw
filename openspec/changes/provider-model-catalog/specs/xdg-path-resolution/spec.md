# XDG Path Resolution Specification

## Purpose

Add the managed model-settings path through the central pure XDG resolver.

## MODIFIED Requirements

### Requirement: XDG Base Directory resolution

`xdg_paths(agent_name)` MUST resolve all agent paths from XDG environment variables and standard fallbacks. It MUST be pure and side-effect free, returning a frozen `XDGPaths` without creating directories or writing files. It SHALL expose `models_config_file` below resolved agent `config_home`; callers SHALL NOT reconstruct it locally.

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
- **AND** `models_config_file` is `~/.config/piclaw/models-config.toml`
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
- **AND** `models_config_file` is `/custom/cfg/piclaw/models-config.toml`
- **AND** data, state, cache, and runtime paths use their standard defaults

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
- **GIVEN** `models_config_file` has been resolved
- **WHEN** application components access the managed settings
- **THEN** it remains under agent `config_home`
- **AND** file tools and the jail do not expose or mount it

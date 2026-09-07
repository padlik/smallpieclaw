# Agent-Scoped Directories Specification

## Purpose

Define `agent_name` as the sole agent identifier (resolved from `--agent-name` CLI); all storage paths derive from XDG Base Directories keyed by `agent_name`; `agent_home` is retired.

## Requirements

### Requirement: agent_name identifies the agent and its XDG storage namespace

`agent_name` MUST be supplied via the `--agent-name` CLI argument. It is NOT read from `[agent].agent_name` config for path derivation. All XDG storage paths are prefixed with `agent_name` (see `xdg-path-resolution` spec), and additionally the agent's operator-facing exchange surfaces live under the dot-home `~/.<agent_name>/` (`skills/`, `results/` — see `path-policy` Tier 1). The `agent_home` concept remains retired.

Feature: Agent-scoped XDG directories
Rule: `agent_name` from `--agent-name` CLI determines all storage paths, both XDG (private state) and dot-home (exchange surfaces).

#### Scenario: Agent named "piclaw" uses XDG paths under that name
- **GIVEN** the agent is started with `--agent-name piclaw`
- **WHEN** the application initialises
- **THEN** all XDG paths are rooted under `piclaw`: `~/.local/state/piclaw/`, `~/.local/share/piclaw/`, `~/.config/piclaw/`, etc.

#### Scenario: The dot-home exchange surface is created under the agent name
- **GIVEN** the agent is started with `--agent-name piclaw`
- **WHEN** the application initialises
- **THEN** `~/.piclaw/skills/` and `~/.piclaw/results/` exist (created if missing)
- **AND** they are distinct from the XDG homes `~/.local/state/piclaw/` and `~/.local/share/piclaw/`

#### Scenario: Different agent names produce distinct XDG namespaces
- **GIVEN** two agents are started: one with `--agent-name piclaw` and one with `--agent-name mybot`
- **WHEN** each agent initialises
- **THEN** their state directories do not overlap: `~/.local/state/piclaw/` and `~/.local/state/mybot/` are distinct
- **AND** no data written by one agent is readable from the other's XDG paths

### Requirement: Vault is always at the XDG state path

The vault file MUST always reside at `$XDG_STATE_HOME/<agent_name>/secrets.toml` (`paths.secrets_file`). It is NOT configurable. The `SPC_VAULT_FILE` environment variable is retired. The `file_vault` config field is removed. No override mechanism exists.

Feature: Vault at XDG state path
Rule: vault is always `~/.local/state/<agent_name>/secrets.toml`; no override exists.

#### Scenario: Vault path derives from agent_name only
- **GIVEN** the agent is started with `--agent-name piclaw`
- **WHEN** the application initialises
- **THEN** the vault is loaded from `~/.local/state/piclaw/secrets.toml`

#### Scenario: No env var or config field can override the vault path
- **GIVEN** the agent is started with `--agent-name piclaw`
- **AND** the environment variable `SPC_VAULT_FILE` is set to `/run/secrets/piclaw.toml`
- **WHEN** the application initialises
- **THEN** the vault is still loaded from `~/.local/state/piclaw/secrets.toml`
- **AND** `SPC_VAULT_FILE` is ignored

### Requirement: Logs are always at the XDG state path

Log files MUST always be written to `$XDG_STATE_HOME/<agent_name>/logs/` (`paths.logs_dir`). The path is NOT configurable via `log_file` in `[paths]`. The `SPC_LOG_DIR` environment variable is retired. No per-agent override exists.

Feature: Logs at XDG state path
Rule: logs are always `~/.local/state/<agent_name>/logs/`; no per-agent override exists.

#### Scenario: Default agent logs at the XDG state path
- **GIVEN** the agent is started with `--agent-name piclaw`
- **WHEN** the application starts
- **THEN** logs are written under `~/.local/state/piclaw/logs/`

#### Scenario: Custom agent name derives log location
- **GIVEN** the agent is started with `--agent-name mybot`
- **WHEN** the application starts
- **THEN** logs are written under `~/.local/state/mybot/logs/`

#### Scenario: Logs are not written into the source checkout directory
- **GIVEN** a default configuration
- **WHEN** the application starts
- **THEN** no `agent.log` is created inside the source checkout directory
- **AND** logs are written to `~/.local/state/<agent_name>/logs/agent.log`

### Requirement: Results memory and legacy long-term memory files are always under XDG data home

`results_memory.json` MUST always reside at `$XDG_DATA_HOME/<agent_name>/results_memory.json`. The legacy, backfill-only `longterm_memory.json` (consumed only by `backfill_graph_memory.py`) MUST default to `$XDG_DATA_HOME/<agent_name>/longterm_memory.json`, overridable via `--longterm-path` for one-off manual runs against a different file. Neither path is configurable via `[paths]` in `config.toml`. The `results_memory_file` and `longterm_memory_file` config fields are removed.

Feature: Generated memory files at XDG data home
Rule: `results_memory.json` is always `paths.data_home / "results_memory.json"`; `longterm_memory.json` defaults to `paths.data_home / "longterm_memory.json"` and is overridable only via the `backfill_graph_memory.py --longterm-path` CLI flag, never via `config.toml`.

#### Scenario: results_memory.json is not configurable
- **GIVEN** `config.toml` sets `[paths] results_memory_file = "/custom/results.json"`
- **WHEN** the application starts with `--agent-name piclaw`
- **THEN** `results_memory.json` is still read from and written to `~/.local/share/piclaw/results_memory.json`
- **AND** the `results_memory_file` config value is ignored

#### Scenario: backfill_graph_memory.py defaults to the XDG data home for longterm_memory.json
- **GIVEN** `backfill_graph_memory.py` is run with `--agent-name piclaw` and no `--longterm-path` override
- **WHEN** the script resolves the source LongTermMemory file
- **THEN** it reads from `~/.local/share/piclaw/longterm_memory.json`

### Requirement: XDG private homes are prohibited for agent file access

The agent's XDG data home (`$XDG_DATA_HOME/<agent_name>/`), XDG state home (`$XDG_STATE_HOME/<agent_name>/`), and config home (`~/.config/<agent_name>/`) MUST be Tier 0 prohibited entries in PathPolicy (see `path-policy`): any `file_*` operation targeting them fails with a `prohibited_path` hard error, and they are never mounted inside the nsjail jail. Access to their contents is exclusively via purpose-built tools and interfaces (`log_query` for the SQLite log store, `memory_*` for memory stores, `secret_get` for the vault, `show_env`-style inspection for config state).

#### Scenario: file_write into the XDG data home is denied
- **GIVEN** the agent is running with agent name `piclaw`
- **WHEN** `file_write` is invoked with a path under `~/.local/share/piclaw/`
- **THEN** the call fails with a `prohibited_path` hard error
- **AND** the suggestion directs the agent to dedicated tools

#### Scenario: Logs are reachable via log_query, not the filesystem
- **GIVEN** the agent needs to inspect past conversation/tool events
- **WHEN** it calls `log_query` with filters
- **THEN** the SQLite structured store returns matching events
- **AND** no filesystem path to the log store is accessible to any file tool or jail process

#### Scenario: The config file is not readable by the agent
- **GIVEN** the agent's config.toml resides at `~/.config/piclaw/config.toml`
- **WHEN** `file_read` is invoked on that path
- **THEN** the call fails with a `prohibited_path` hard error (the config may hold credentials)

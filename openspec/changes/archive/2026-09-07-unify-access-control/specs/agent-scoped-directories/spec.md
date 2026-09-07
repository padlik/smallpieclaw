# agent-scoped-directories Delta

## Purpose

The agent gains a dot-home (`~/.<agent>/`) holding its operator-facing exchange surfaces (skills, results), while the XDG data/state/config homes remain the private state namespace — now Tier 0 prohibited for agent file access. XDG path *resolution* is unchanged; only access policy changes.

## MODIFIED Requirements

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

## ADDED Requirements

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
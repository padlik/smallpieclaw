# Agent-Scoped Directories Specification

## Purpose

Define `agent_name` as the sole agent identifier (resolved from `--agent-name` CLI); all storage paths derive from XDG Base Directories keyed by `agent_name`; `agent_home` is retired.

## Requirements

### Requirement: agent_name identifies the agent and its XDG storage namespace

`agent_name` MUST be supplied via the `--agent-name` CLI argument. It is NOT read from `[agent].agent_name` config for path derivation. All XDG storage paths are prefixed with `agent_name` (see `xdg-path-resolution` spec). The `agent_home` concept is retired; no config field or env var provides it.

Feature: Agent-scoped XDG directories
Rule: `agent_name` from `--agent-name` CLI determines all storage paths; `agent_home` concept is retired.

#### Scenario: Agent named "piclaw" uses XDG paths under that name
- **GIVEN** the agent is started with `--agent-name piclaw`
- **WHEN** the application initialises
- **THEN** all XDG paths are rooted under `piclaw`: `~/.local/state/piclaw/`, `~/.local/share/piclaw/`, `~/.config/piclaw/`, etc.

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

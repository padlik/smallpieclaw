# Agent-Scoped Directories Specification

## Purpose

Define `agent_name` and `agent_home` configuration fields for shared agent state paths.

## Requirements

### Requirement: Agent name and home directory configuration

The application MUST support `agent_name` and `agent_home` fields in `[agent]` config, with sensible defaults. The vault path is independent of `agent_home`.

Feature: Agent-scoped directories
Rule: `agent_name` determines `agent_home`; vault lives in `~/.local/share/<agent_name>/` regardless of `agent_home`.

#### Scenario: Default agent name and home
- **GIVEN** `[agent]` does not specify `agent_name` or `agent_home`
- **WHEN** the application starts
- **THEN** `agent_name` defaults to `"piclaw"`
- **AND** `agent_home` resolves to `~/piclaw/`
- **AND** the vault path defaults to `~/.local/share/piclaw/secrets.toml`

#### Scenario: Custom agent name with default home
- **GIVEN** `[agent]` has `agent_name = "mybot"`
- **AND** `agent_home` is not set
- **WHEN** the application starts
- **THEN** `agent_home` resolves to `~/mybot/`
- **AND** the vault path defaults to `~/.local/share/mybot/secrets.toml`

#### Scenario: Explicit agent home does NOT affect vault path
- **GIVEN** `[agent]` has `agent_home = "/opt/smallpieclaw/data"`
- **WHEN** the application starts
- **THEN** `agent_home` is `/opt/smallpieclaw/data`
- **AND** the vault path still defaults to `~/.local/share/piclaw/secrets.toml`
- **AND** the vault path is unaffected by `agent_home`

#### Scenario: Vault path overridden by environment variable
- **GIVEN** `[agent]` has `agent_name = "mybot"`
- **AND** `agent_home` defaults to `~/mybot/`
- **AND** the environment variable `SPC_VAULT_FILE` is set to `/run/secrets/mybot.toml`
- **WHEN** the application starts
- **THEN** the vault is loaded from `/run/secrets/mybot.toml`
- **AND** the default `~/.local/share/mybot/secrets.toml` is ignored

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

### Requirement: Log files live in an XDG state directory

The application MUST write log files to an XDG state directory derived from `agent_name`, resolved independently of `agent_home`. This extends the existing rule that agent-scoped state paths derive from `agent_name`, using the XDG *state* directory for logs (parallel to the vault's use of the XDG *data* directory).

Feature: Agent-scoped log directory
Rule: Logs default to `~/.local/state/<agent_name>/logs/` regardless of `agent_home`; an explicit absolute `log_file` overrides.

#### Scenario: Default log location for default agent
- **GIVEN** `[agent]` does not specify `agent_name` or `agent_home`
- **AND** `[paths]` does not set an absolute `log_file`
- **WHEN** the application starts
- **THEN** logs are written under `~/.local/state/piclaw/logs/`

#### Scenario: Custom agent name derives log location
- **GIVEN** `[agent]` has `agent_name = "mybot"`
- **WHEN** the application starts
- **THEN** logs are written under `~/.local/state/mybot/logs/`

#### Scenario: Explicit agent home does NOT affect log location
- **GIVEN** `[agent]` has `agent_home = "/opt/smallpieclaw/data"`
- **WHEN** the application starts
- **THEN** the log location still defaults under `~/.local/state/piclaw/logs/`
- **AND** the log location is unaffected by `agent_home`

#### Scenario: Explicit absolute log_file overrides the default
- **GIVEN** `[paths]` sets `log_file = "/var/log/piclaw/agent.log"`
- **WHEN** the application starts
- **THEN** logs are written to `/var/log/piclaw/agent.log`
- **AND** the XDG state default is ignored

#### Scenario: Logs are no longer written into the source checkout
- **GIVEN** a default configuration
- **WHEN** the application starts
- **THEN** no `agent.log` is created inside the source checkout directory

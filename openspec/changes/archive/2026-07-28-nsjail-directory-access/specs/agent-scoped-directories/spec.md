## MODIFIED Requirements

### Requirement: Agent name and home directory configuration

The application MUST support `agent_name` and `agent_home` fields in `[agent]` config, with sensible defaults. The vault path is independent of `agent_home` and lives under `XDG_STATE_HOME` alongside other agent state files.

Feature: Agent-scoped directories
Rule: `agent_name` determines `agent_home`; vault lives in `~/.local/state/<agent_name>/secrets.toml` regardless of `agent_home`.

#### Scenario: Default agent name and home
- **GIVEN** `[agent]` does not specify `agent_name` or `agent_home`
- **WHEN** the application starts
- **THEN** `agent_name` defaults to `"piclaw"`
- **AND** `agent_home` resolves to `~/piclaw/`
- **AND** the vault path defaults to `~/.local/state/piclaw/secrets.toml`

#### Scenario: Custom agent name with default home
- **GIVEN** `[agent]` has `agent_name = "mybot"`
- **AND** `agent_home` is not set
- **WHEN** the application starts
- **THEN** `agent_home` resolves to `~/mybot/`
- **AND** the vault path defaults to `~/.local/state/mybot/secrets.toml`

#### Scenario: Explicit agent home does NOT affect vault path
- **GIVEN** `[agent]` has `agent_home = "/opt/smallpieclaw/data"`
- **WHEN** the application starts
- **THEN** `agent_home` is `/opt/smallpieclaw/data`
- **AND** the vault path still defaults to `~/.local/state/piclaw/secrets.toml`
- **AND** the vault path is unaffected by `agent_home`

#### Scenario: Vault path overridden by environment variable
- **GIVEN** `[agent]` has `agent_name = "mybot"`
- **AND** `agent_home` defaults to `~/mybot/`
- **AND** the environment variable `SPC_VAULT_FILE` is set to `/run/secrets/mybot.toml`
- **WHEN** the application starts
- **THEN** the vault is loaded from `/run/secrets/mybot.toml`
- **AND** the default `~/.local/state/mybot/secrets.toml` is ignored

#### Scenario: Vault migrated from old XDG_DATA_HOME location
- **GIVEN** a vault file exists at the old path `~/.local/share/<agent_name>/secrets.toml`
- **AND** no vault file exists at the new path `~/.local/state/<agent_name>/secrets.toml`
- **WHEN** the application starts
- **THEN** the old vault file is copied to the new path
- **AND** an info log records the migration
- **AND** the old file remains on disk (non-destructive copy)

#### Scenario: Both old and new vault paths exist
- **GIVEN** a vault file exists at the old path `~/.local/share/<agent_name>/secrets.toml`
- **AND** a vault file also exists at the new path `~/.local/state/<agent_name>/secrets.toml`
- **WHEN** the application starts
- **THEN** the vault is loaded from the new path
- **AND** a warning is logged that the old path is stale and can be removed manually
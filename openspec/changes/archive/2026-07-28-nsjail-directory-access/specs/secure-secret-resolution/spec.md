## MODIFIED Requirements

### Requirement: File-backed vault storage

The application MUST support a `[vault]` config section with a `type` field, and SHALL load key-value pairs from a TOML file at startup when `type = "file"`. The default vault path is `~/.local/state/<agent_name>/secrets.toml` (XDG_STATE_HOME), consolidated alongside other agent state files.

Feature: Vault secret manager
Rule: Agent-scoped secrets live in a single TOML file under XDG_STATE_HOME, referenced by key name.

#### Scenario: Vault file exists and contains secrets
- **GIVEN** the `[vault]` section has `type = "file"`
- **AND** a TOML file exists at `~/.local/state/<agent_name>/secrets.toml`
- **WHEN** the application starts
- **THEN** the vault is loaded into memory
- **AND** keys are accessible by exact name

#### Scenario: Vault file is missing
- **GIVEN** the config contains at least one `sec:` reference
- **AND** the vault file does not exist
- **WHEN** the application parses the configuration
- **THEN** startup fails with a clear error indicating the vault file is missing

#### Scenario: Vault file contains invalid TOML
- **GIVEN** the vault file exists but is not valid TOML
- **WHEN** the application attempts to load the vault
- **THEN** startup fails with a clear error indicating the vault file is corrupt

#### Scenario: Vault file is overridden by environment variable
- **GIVEN** the environment variable `SPC_VAULT_FILE` is set to a valid path
- **AND** the `[vault]` section has `type = "file"`
- **WHEN** the application starts
- **THEN** the vault is loaded from the path in `SPC_VAULT_FILE`
- **AND** the default `~/.local/state/<agent_name>/secrets.toml` is ignored
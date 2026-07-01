# Vault Secret Manager Specification

## Purpose

Define a centralized, agent-scoped vault for storing arbitrary string values (API keys, tokens, URLs, bearer headers) in a single JSON file.

## ADDED Requirements

### Requirement: File-backed vault storage

The application MUST support a `[vault]` config section with a `type` field, and SHALL load key-value pairs from a JSON file at startup when `type = "file"`.

Feature: Vault secret manager
Rule: Agent-scoped secrets live in a single JSON file, referenced by key name.

#### Scenario: Vault file exists and contains secrets
- **GIVEN** the `[vault]` section has `type = "file"`
- **AND** a JSON file exists at `~/.local/share/<agent_name>/secrets.json`
- **WHEN** the application starts
- **THEN** the vault is loaded into memory
- **AND** keys are accessible by exact name

#### Scenario: Vault file is missing
- **GIVEN** the config contains at least one `sec:` reference
- **AND** the vault file does not exist
- **WHEN** the application parses the configuration
- **THEN** startup fails with a clear error indicating the vault file is missing

#### Scenario: Vault file contains invalid JSON
- **GIVEN** the vault file exists but is not valid JSON
- **WHEN** the application attempts to load the vault
- **THEN** startup fails with a clear error indicating the vault file is corrupt

#### Scenario: Vault file is overridden by environment variable
- **GIVEN** the environment variable `SPC_VAULT_FILE` is set to a valid path
- **AND** the `[vault]` section has `type = "file"`
- **WHEN** the application starts
- **THEN** the vault is loaded from the path in `SPC_VAULT_FILE`
- **AND** the default `~/.local/share/<agent_name>/secrets.json` is ignored

### Requirement: Vault supports arbitrary string values

The vault SHALL store any string value, not just secrets. Values are opaque strings — the application does not inspect or validate them.

Feature: Vault secret manager
Rule: Vault values are plain strings; any key can hold a key, token, URL, or header.

#### Scenario: Vault contains a base URL
- **GIVEN** the vault contains `"OLLAMA_HOST": "http://localhost:11434"`
- **WHEN** a config field uses `sec:OLLAMA_HOST`
- **THEN** the resolved value is `"http://localhost:11434"`

#### Scenario: Vault contains a bearer header
- **GIVEN** the vault contains `"AUTH_HEADER": "Bearer sk-xyz"`
- **WHEN** a config field uses `sec:AUTH_HEADER`
- **THEN** the resolved value is `"Bearer sk-xyz"`

## MODIFIED Requirements

## REMOVED Requirements

### Requirement: File-backed secret resolution
**Reason**: Replaced by the unified `sec:` vault prefix. The `api_key_file`/`bot_token_file` mechanism is removed.

**Migration**: Move secret file contents into the agent vault (`~/.local/share/<agent_name>/secrets.json`) and replace `api_key_file = "..."` with `api_key = "sec:KEY_NAME"`.

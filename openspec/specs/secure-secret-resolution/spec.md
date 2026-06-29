# Secure Secret Resolution Specification

## Purpose

Define file-backed secret resolution and production deployment guidance for reducing secret exposure in environment variables.

## Requirements

### Requirement: File-backed secret resolution
The application MUST support resolving supported sensitive string configuration fields from protected secret files, and SHALL fail startup with a clear configuration error when a requested secret file cannot produce a valid secret value.

Feature: Secure secret resolution
Rule: Sensitive config values may be loaded from protected files instead of environment values.

#### Scenario: Secret value is loaded from a configured file path
- **GIVEN** a string secret field is configured with a readable secret file path
- **WHEN** the application parses the configuration
- **THEN** the field is resolved to the contents of the secret file
- **AND** one trailing newline from the file does not become part of the secret value

#### Scenario: Secret file path is supplied by environment variable
- **GIVEN** a string secret file field references an environment variable containing a file path
- **AND** the referenced file contains a non-empty secret
- **WHEN** the application parses the configuration
- **THEN** the field is resolved to the secret file contents
- **AND** the environment variable does not need to contain the secret value itself

#### Scenario: Missing secret file fails startup clearly
- **GIVEN** a string secret file field points to a file that does not exist
- **WHEN** the application parses the configuration
- **THEN** startup fails with a configuration error identifying the affected field and missing file

#### Scenario: Empty secret file fails startup clearly
- **GIVEN** a string secret file field points to a readable file with no secret value
- **WHEN** the application parses the configuration
- **THEN** startup fails with a configuration error identifying the affected field

#### Scenario: Same-level value and file secret sources are rejected
- **GIVEN** a supported secret field is configured with both a direct value and a file path at the same configuration level
- **WHEN** the application parses the configuration
- **THEN** startup fails with a configuration error identifying the ambiguous secret source fields

#### Scenario: Intentional secret whitespace is preserved
- **GIVEN** a string secret file contains leading or trailing spaces that are part of the secret value
- **WHEN** the application parses the configuration
- **THEN** the resolved secret preserves the intentional spaces
- **AND** at most one trailing newline sequence from the file is removed

### Requirement: Systemd user service credential guidance
Production documentation MUST include a `systemd --user` credential-file pattern for supported secrets and SHALL describe environment-value injection as a less secure compatibility fallback.

Feature: Secure secret resolution
Rule: Production documentation should guide operators toward file-path environment variables for systemd user services.

#### Scenario: Operator configures a user service with systemd credentials
- **GIVEN** an operator runs the application as a `systemd --user` service
- **WHEN** they follow the production deployment documentation
- **THEN** the example service uses `LoadCredential=` for API keys or bot tokens
- **AND** the example exposes credential file paths through environment variables
- **AND** the example preserves a correct `WorkingDirectory=` for config and relative paths

#### Scenario: Operator needs a compatibility fallback
- **GIVEN** an operator cannot use systemd credentials on the target host
- **WHEN** they read the production deployment documentation
- **THEN** the documentation describes environment-value secret injection as a fallback
- **AND** the documentation calls out that env-value secrets are inherited by subprocesses and may be exposed through process environment inspection

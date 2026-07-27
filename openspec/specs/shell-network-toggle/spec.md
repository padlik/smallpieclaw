### Requirement: Shell network access is controlled by a boolean allow_net config field

The agent configuration MUST use a boolean `allow_net` field (default `false`) to control whether nsjail shell commands have network access. When `false`, network is isolated (`clone_newnet: true`). When `true`, the jail shares the host network namespace (`clone_newnet: false`). This replaces the legacy string `shell_nsjail_network` field.

Feature: shell-network-toggle

#### Scenario: Default config isolates network
- **GIVEN** `allow_net` is not set in the agent configuration
- **WHEN** the agent starts with `shell_backend = "nsjail"`
- **THEN** `allow_net` defaults to `false`
- **AND** the generated nsjail config sets `clone_newnet: true`
- **AND** shell commands inside the jail have no network access

#### Scenario: allow_net true enables host network
- **GIVEN** `allow_net` is set to `true` in the agent configuration
- **WHEN** the nsjail config is generated for a shell command
- **THEN** `clone_newnet` is set to `false`
- **AND** the jail shares the host network namespace
- **AND** shell commands can access the network subject to host networking rules

#### Scenario: Legacy shell_nsjail_network field is rejected
- **GIVEN** the configuration contains `shell_nsjail_network` (any value)
- **WHEN** the agent parses the configuration at startup
- **THEN** a `ConfigError` is raised with a clear migration message directing the operator to replace it with `allow_net = true/false`
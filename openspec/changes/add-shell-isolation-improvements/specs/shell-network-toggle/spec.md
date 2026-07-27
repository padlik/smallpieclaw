## ADDED Requirements

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

## MODIFIED Requirements

### Requirement: Network is isolated by default

The nsjail config MUST set `clone_newnet: true` by default, giving the jail an empty network namespace with no network access. The `allow_net` config field controls whether the network namespace is created: when set to `false` (default), `clone_newnet` is `true` (network isolated, no access); when set to `true`, `clone_newnet` is `false` (jail shares the host network namespace, no network isolation). Future connectivity options (pasta userland NAT, loopback-only) are out of scope.

#### Scenario: Default config has no network
- **GIVEN** `allow_net` is not set (defaults to `false`)
- **WHEN** the agent runs `shell("curl https://example.com")` inside the jail
- **THEN** the command fails with a network error (empty network namespace, no interfaces)

#### Scenario: Network isolation can be disabled via config
- **GIVEN** `allow_net` is set to `true`
- **WHEN** the nsjail config is generated
- **THEN** `clone_newnet` is set to `false`
- **AND** the jail shares the host network namespace (network access is available, subject to host networking)

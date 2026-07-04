## ADDED Requirements

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

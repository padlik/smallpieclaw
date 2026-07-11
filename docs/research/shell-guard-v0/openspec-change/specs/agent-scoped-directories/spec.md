## MODIFIED Requirements

### Requirement: Log files live in an XDG state directory

The application MUST write log files and Shell Guard detailed metadata events to an XDG state directory derived from `agent_name`, resolved independently of `agent_home`. This extends the existing rule that agent-scoped state paths derive from `agent_name`, using the XDG *state* directory for logs and guard telemetry (parallel to the vault's use of the XDG *data* directory).

Feature: Agent-scoped log directory
Rule: Logs default to `~/.local/state/<agent_name>/logs/` regardless of `agent_home`; Shell Guard metadata defaults under `~/.local/state/<agent_name>/shell_guard/`; an explicit absolute `log_file` overrides normal log files only.

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
- **AND** the XDG state default is ignored for normal log files

#### Scenario: Logs are no longer written into the source checkout
- **GIVEN** a default configuration
- **WHEN** the application starts
- **THEN** no `agent.log` is created inside the source checkout directory

#### Scenario: Shell Guard metadata uses XDG state
- **GIVEN** `[agent]` has `agent_name = "mybot"`
- **WHEN** Shell Guard writes detailed metadata events
- **THEN** the metadata JSONL file is written under `~/.local/state/mybot/shell_guard/`
- **AND** the metadata location is independent of `agent_home`

## ADDED Requirements

### Requirement: Shell Guard policy and artifacts use agent-scoped storage
Shell Guard SHALL store policy files, policy backups, and bulky guard artifacts under agent-scoped local storage derived from `agent_name`, and the daemon and CLI SHALL resolve the same locations. By default, policy files, policy backups, and local classification evidence artifacts use XDG data storage under `~/.local/share/<agent_name>/shell_guard/`, while detailed metadata events use XDG state storage under `~/.local/state/<agent_name>/shell_guard/`.

#### Scenario: Shell Guard policy defaults under agent-scoped storage
- **GIVEN** `[agent]` has `agent_name = "mybot"`
- **AND** no explicit Shell Guard policy path is configured
- **WHEN** Shell Guard loads policy
- **THEN** the default policy path MUST resolve under `~/.local/share/mybot/shell_guard/`
- **AND** it MUST NOT default to the source checkout directory

#### Scenario: Shell Guard CLI and daemon share locations
- **GIVEN** Shell Guard classify metadata has been written by the daemon
- **WHEN** the user runs `python -m shell_guard policy candidates` with the same agent identity
- **THEN** the CLI MUST read classify observations from the same agent-scoped metadata location
- **AND** policy apply MUST write the policy file used by the daemon for that agent identity

#### Scenario: Policy apply creates agent-scoped backups
- **GIVEN** Shell Guard policy apply mutates the active policy file
- **WHEN** the apply completes
- **THEN** the previous policy file MUST be copied to a timestamped backup under `~/.local/share/<agent_name>/shell_guard/`

#### Scenario: Local evidence artifacts are owner-only
- **GIVEN** Shell Guard stores local referenced-script content or bulky classification evidence for debugging or advisor context
- **WHEN** the artifact is written
- **THEN** it MUST be stored under an artifact directory below `~/.local/share/<agent_name>/shell_guard/`
- **AND** the file and directory permissions MUST be restricted to the owner where the platform supports it

## REMOVED Requirements

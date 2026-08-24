## MODIFIED Requirements

### Requirement: Public Telegram command discovery excludes removed and hidden commands

The system SHALL expose only supported user-facing Telegram slash commands through Telegram command discovery and built-in help text. The `/context` command SHALL be included in the visible command surface.

#### Scenario: Telegram command menu omits health and compress
- **WHEN** the Telegram interface registers bot commands during startup
- **THEN** the registered command list MUST NOT include `/health`
- **AND** the registered command list MUST NOT include `/compress`

#### Scenario: Help text omits health and compress
- **WHEN** an authorized user requests `/help`
- **THEN** the help text MUST NOT list `/health`
- **AND** the help text MUST NOT list `/compress`
- **AND** the help text MUST continue to list `/status` and `/reset`

#### Scenario: Telegram command menu includes context
- **WHEN** the Telegram interface registers bot commands during startup
- **THEN** the registered command list MUST include `/context`
- **AND** the registered command list MUST NOT include `/health`
- **AND** the registered command list MUST NOT include `/compress`

#### Scenario: Help text includes context
- **WHEN** an authorized user requests `/help`
- **THEN** the help text MUST include `/context`
- **AND** the help text MUST NOT list `/health`
- **AND** the help text MUST NOT list `/compress`
- **AND** the help text MUST continue to list `/status` and `/reset`
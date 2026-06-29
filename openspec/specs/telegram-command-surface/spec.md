# telegram-command-surface Specification

## Purpose
Define the supported user-visible Telegram slash command surface and the behavior for hidden and advanced commands. This specification governs which commands appear in Telegram command discovery, which are registered but hidden, and how help text reflects the public command surface.

## Requirements

### Requirement: Public Telegram command discovery excludes removed and hidden commands
The system SHALL expose only supported user-facing Telegram slash commands through Telegram command discovery and built-in help text.

#### Scenario: Telegram command menu omits health and compress
- **WHEN** the Telegram interface registers bot commands during startup
- **THEN** the registered command list MUST NOT include `/health`
- **AND** the registered command list MUST NOT include `/compress`

#### Scenario: Help text omits health and compress
- **WHEN** an authorized user requests `/help`
- **THEN** the help text MUST NOT list `/health`
- **AND** the help text MUST NOT list `/compress`
- **AND** the help text MUST continue to list `/status` and `/reset`

### Requirement: Health slash command is removed
The system SHALL NOT provide `/health` as a Telegram slash command or hidden command handler.

#### Scenario: Health handler is not registered
- **WHEN** the Telegram interface registers command handlers
- **THEN** no handler MUST be registered for the `health` command

#### Scenario: Health diagnosis remains available without slash command
- **WHEN** a user wants a health diagnosis after `/health` removal
- **THEN** documentation MUST direct them to use a natural-language request or a scheduled job pattern instead of `/health`

### Requirement: Compress remains hidden and functional
The system SHALL keep manual context compression available as an advanced hidden Telegram command while excluding it from normal command discovery and help.

#### Scenario: Compress handler remains registered
- **WHEN** the Telegram interface registers command handlers
- **THEN** a handler MUST still be registered for the `compress` command

#### Scenario: Compress is documented as advanced behavior only
- **WHEN** documentation describes context compaction
- **THEN** automatic compaction MUST be presented as the normal context-window protection mechanism
- **AND** `/compress` MUST NOT appear in the primary public command table

### Requirement: Reset remains the visible context lifecycle command
The system SHALL keep `/reset` visible and documented as the user-facing command for saving or discarding task context and starting fresh.

#### Scenario: Reset remains discoverable
- **WHEN** the Telegram interface registers bot commands during startup
- **THEN** the registered command list MUST include `/reset`

#### Scenario: Reset remains in help and documentation
- **WHEN** an authorized user requests `/help`
- **THEN** the help text MUST include `/reset`
- **AND** documentation MUST continue to describe `/reset` and `/reset discard`

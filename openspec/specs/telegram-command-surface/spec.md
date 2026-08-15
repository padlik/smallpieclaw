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

### Requirement: Agents command shows source-aware running agents
The `/agents` command SHALL display visible running sub-agent executions with distinct source/category labels.

#### Scenario: Agents list shows source labels
- **GIVEN** active visible agent records exist for multiple source categories
- **WHEN** an authorized operator runs `/agents`
- **THEN** the response includes each visible record
- **AND** each record shows whether it is `on-demand`, `scheduled`, `plan-step`, or `diagnostic`

#### Scenario: Managed cancellation help describes capacity scope
- **GIVEN** an authorized operator views `/agents` help or an empty `/agents` list
- **WHEN** the response mentions managed cancellation
- **THEN** it explains that managed cancellation applies to globally capacity-counted sources

#### Scenario: Explicit cancellation works for all visible sources
- **GIVEN** a visible running agent has any supported source category
- **WHEN** an authorized operator runs `/agents cancel <id-or-label>` for that record
- **THEN** cancellation is requested for the matching active record

### Requirement: Status command active-agent count uses visible registry records
The `/status` command SHALL report the total number of active visible sub-agent records in the global registry.

#### Scenario: Status count includes plan-step and diagnostic records
- **GIVEN** active registry records exist for `on-demand`, `scheduled`, `plan-step`, and `diagnostic` sources
- **WHEN** an authorized operator requests `/status`
- **THEN** the active-agent count includes all visible registered records
- **AND** the count is not limited to globally capacity-counted records

### Requirement: /stop cascades to all sub-agents

The `/stop` command SHALL cancel the main agent AND all on-demand and scheduled sub-agents by calling `cancel_all_managed()`. Plan-step and diagnostic sub-agents are already cancelled via the existing `PlanExecutor` bridge thread.

Feature: Telegram command surface
Rule: `/stop` means "stop everything" — the operator's "things are gone wrong" escape hatch.

#### Scenario: /stop cancels on-demand sub-agents
- **GIVEN** the main agent is running and sub-agents A (on-demand) and B (on-demand) are running in parallel
- **WHEN** the operator runs `/stop`
- **THEN** the main agent's cancel event is set
- **AND** A and B are cancelled via `cancel_all_managed()`
- **AND** the operator sees a confirmation that the main agent and all sub-agents are cancelling

#### Scenario: /stop cancels scheduled sub-agents
- **GIVEN** a scheduled sub-agent S is running
- **WHEN** the operator runs `/stop`
- **THEN** S is cancelled via `cancel_all_managed()`

#### Scenario: /stop cancels plan-step sub-agents via the existing bridge
- **GIVEN** the main agent is running an execution plan and plan-step sub-agents P1, P2 are running
- **WHEN** the operator runs `/stop`
- **THEN** the main agent's cancel event is set
- **AND** the PlanExecutor's bridge thread notices within 100ms and sets the plan's cancel event
- **AND** P1 and P2 are cancelled by the plan executor's own cancellation logic

### Requirement: /prompts command is registered and discoverable

The `/prompts` command SHALL be registered as a Telegram slash command and listed in help text. The command accepts three forms: bare `/prompts` (list recent), `/prompts search <query> [Nd/Nh] [--status=<S>] [--trace=<T>] [--since=<ISO>] [--until=<ISO>] [--page=<N>]` (text search with optional filters), and `/prompts show <id>` (display a single prompt's full record). The content contract for the list view is owned by the `prompt-tracking` capability; the search and show content contracts are also owned by `prompt-tracking`.

Feature: Telegram command surface
Rule: This requirement governs discovery and surface only; the list's fields, search behavior, and show display are specified in `prompt-tracking`.

#### Scenario: /prompts is registered as a command
- **WHEN** the Telegram interface registers bot commands during startup
- **THEN** the registered command list includes `/prompts`

#### Scenario: /prompts appears in help
- **WHEN** an authorized user requests `/help`
- **THEN** the help text includes `/prompts`

#### Scenario: /prompts help mentions search and show subcommands
- **WHEN** an authorized user requests `/help`
- **THEN** the help text mentions `/prompts search <query> [Nd/Nh]` for searching prompt history
- **AND** the help text mentions the filter flags `--status`, `--trace`, `--since`, `--until`, and `--page`
- **AND** the help text mentions `/prompts show <id>` for viewing a single prompt's details

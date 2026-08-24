## ADDED Requirements

### Requirement: /resume command is registered and discoverable

The `/resume` command SHALL be registered as a Telegram slash command and listed in help text. The command loads disk-persisted checkpoints and resumes interrupted runs. The checkpoint lifecycle, error card rendering, and resume behavior are owned by the `llm-error-recovery` capability; this requirement governs discovery and surface only.

Feature: Telegram command surface
Rule: `/resume` is the user-facing command for recovering interrupted runs from disk checkpoints.

#### Scenario: /resume is registered as a command
- **WHEN** the Telegram interface registers bot commands during startup
- **THEN** the registered command list includes `/resume`

#### Scenario: /resume appears in help
- **WHEN** an authorized user requests `/help`
- **THEN** the help text includes `/resume`
- **AND** the help text describes `/resume` as "Resume an interrupted run from a saved checkpoint"
- **AND** the help text mentions `/resume N` for selecting a specific checkpoint when multiple exist
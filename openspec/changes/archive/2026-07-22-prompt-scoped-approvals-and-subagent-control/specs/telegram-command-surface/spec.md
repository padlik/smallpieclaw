## ADDED Requirements

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

The `/prompts` command SHALL be registered as a Telegram slash command and listed in help text. The content contract (what the list shows) is owned by the `prompt-tracking` capability.

Feature: Telegram command surface
Rule: This requirement governs discovery and surface only; the list's fields and default count are specified in `prompt-tracking`.

#### Scenario: /prompts is registered as a command
- **WHEN** the Telegram interface registers bot commands during startup
- **THEN** the registered command list includes `/prompts`

#### Scenario: /prompts appears in help
- **WHEN** an authorized user requests `/help`
- **THEN** the help text includes `/prompts`
# telegram-command-surface Specification

## Purpose

Extend the established Telegram `/models` surface without changing unrelated public, hidden, or advanced commands.

## MODIFIED Requirements

### Requirement: Public Telegram command discovery excludes removed and hidden commands

Telegram discovery and help SHALL expose supported public commands, including `/context` and the existing `/models` command. Catalog interaction extends `/models`; it SHALL NOT add duplicate catalog commands or change hidden/advanced command visibility.

#### Scenario: Telegram command menu omits health and compress
- **WHEN** the interface registers commands during startup
- **THEN** the registered list excludes `/health` and `/compress`

#### Scenario: Help text omits health and compress
- **WHEN** an authorized user requests `/help`
- **THEN** help excludes `/health` and `/compress`
- **AND** help continues to list `/status` and `/reset`

#### Scenario: Telegram command menu includes context
- **WHEN** the interface registers commands during startup
- **THEN** the registered list includes `/context`
- **AND** excludes `/health` and `/compress`

#### Scenario: Help text includes context
- **WHEN** an authorized user requests `/help`
- **THEN** help includes `/context` and excludes `/health` and `/compress`
- **AND** help continues to list `/status` and `/reset`

#### Scenario: Existing model command remains the catalog entry point
- **WHEN** authorized command discovery and help are rendered
- **THEN** existing `/models` is the only catalog command
- **AND** unrelated command visibility is unchanged

## ADDED Requirements

### Requirement: `/models` lists, refreshes, and selects discovered models

Authorized `/models` SHALL list about 20 entries simply, refresh all providers, show fresh/stale inventory, selection state, and known/unknown/unsupported capabilities. It SHALL distinguish provider metadata from assumed budgets and never promise callability. Selection SHALL use an opaque callback no longer than 64 bytes, revalidate expiry and inventory, and persist before publishing success.

#### Scenario: `/models` presents a concise catalog
- **GIVEN** a catalog contains about 20 models with fresh/stale and known/unknown metadata
- **WHEN** an authorized user invokes `/models`
- **THEN** the list shows available and active/saved-unavailable selection state
- **AND** capability and budget-source states are distinguished
- **AND** no general pagination framework is introduced

#### Scenario: Refresh failure preserves stale inventory
- **GIVEN** a provider refresh fails
- **WHEN** `/models` displays the catalog
- **THEN** its last-good entries remain selectable and marked stale
- **AND** the failure does not disable unrelated commands

#### Scenario: Callback payload does not include model identity
- **GIVEN** a canonical model ID is longer than Telegram's callback limit
- **WHEN** the selection button is rendered
- **THEN** it carries only an opaque token of at most 64 bytes
- **AND** the token resolves to the canonical ID server-side

#### Scenario: Expired or removed selection token is rejected
- **GIVEN** a selection callback expired or its model disappeared
- **WHEN** the callback is used
- **THEN** active selection is unchanged
- **AND** the operator is asked to reopen `/models`

#### Scenario: Selection persistence fails
- **GIVEN** a current discovered model is selected but settings cannot be persisted
- **WHEN** selection completes
- **THEN** the old active selection remains
- **AND** no success is reported

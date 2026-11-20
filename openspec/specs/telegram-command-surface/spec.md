# telegram-command-surface Specification

## Purpose
Define the supported user-visible Telegram slash command surface and the behavior for hidden and advanced commands. This specification governs which commands appear in Telegram command discovery, which are registered but hidden, and how help text reflects the public command surface.

## Requirements
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

### Requirement: Scheduled job history view is read-only and honest

The authenticated `/jobs history <tag>` view SHALL read retained **job execution records** directly and MUST NOT invoke the operator agent, create a prompt, or touch operator memory. It SHALL show retained run IDs, statuses, and timestamps, including removed once-jobs. Legacy records without execution IDs SHALL be identified as legacy/partial, with no fabricated IDs or completeness claim.

#### Scenario: History lists retained runs directly
- **GIVEN** an authenticated operator requests `/jobs history <tag>` for a tag with retained executions
- **WHEN** the command is handled
- **THEN** the response lists each retained execution ID, status, and timestamp
- **AND** no operator agent turn or memory access occurs

#### Scenario: Removed once-job remains visible in history
- **GIVEN** a once-job definition has been removed but its execution records remain retained
- **WHEN** the operator requests `/jobs history <tag>`
- **THEN** its retained run IDs, statuses, and timestamps are listed

#### Scenario: Legacy history is not fabricated as complete
- **GIVEN** a retained legacy record has no execution ID and contains truncated output
- **WHEN** it appears in `/jobs history <tag>`
- **THEN** it is identified as legacy/partial with its mapped status and timestamp
- **AND** no execution ID or missing output is fabricated

### Requirement: Scheduled job result view is read-only and complete

The authenticated `/jobs result <execution-id>` command SHALL present one complete retained outcome directly from its **job execution record**, without invoking the operator agent, creating an operator prompt, or reading or modifying operator memory. A result exceeding Telegram's message limit SHALL be delivered as a complete UTF-8 text document. Unknown or expired execution IDs SHALL report that the result is unavailable.

#### Scenario: Result command returns complete retained outcome
- **GIVEN** an authenticated operator requests `/jobs result <execution-id>` for a retained record
- **WHEN** the complete result fits within Telegram's message limit
- **THEN** the command returns the full result without truncation
- **AND** it does not invoke the operator agent or alter context

#### Scenario: Large result is delivered as a document
- **GIVEN** an authenticated operator requests a retained result larger than Telegram's message limit
- **WHEN** the command presents the result
- **THEN** it delivers the complete result as a UTF-8 text document
- **AND** it does not truncate the result or invoke the operator agent

#### Scenario: Unknown or expired result is unavailable
- **GIVEN** an authenticated operator requests `/jobs result <execution-id>` for an unknown or expired ID
- **WHEN** the command performs the lookup
- **THEN** it reports that the result is unavailable
- **AND** it does not access operator memory or create a prompt

### Requirement: Scheduled job history distinguishes unknown, empty, and removed tags

For `/jobs history <tag>`, a tag with no active definition and no retained runs SHALL be reported unknown; an active job with no retained runs SHALL be reported as empty history; a removed once-job with retained records SHALL remain known until those records expire.

#### Scenario: Unknown tag history is reported
- **GIVEN** an authenticated operator requests history for a tag with no active definition and no retained runs
- **WHEN** the command looks up the tag
- **THEN** the command reports the tag as unknown

#### Scenario: Empty history is reported for an active job
- **GIVEN** an authenticated operator requests history for a tag with an active definition but no retained runs
- **WHEN** the command looks up the tag
- **THEN** the command reports empty history

### Requirement: Job history and result commands are authenticated and discoverable

`/jobs history` and `/jobs result` SHALL be available only to the authenticated operator and SHALL be documented as read-only views in `/help` and `/jobs` usage. A missing required argument SHALL return usage text without invoking the operator agent. Existing `/jobs`, `/agents`, and `/stop` contracts MUST remain unchanged.

#### Scenario: Missing arguments show command usage
- **GIVEN** an authenticated operator invokes `/jobs history` or `/jobs result` without its required argument
- **WHEN** the command is handled
- **THEN** it returns the corresponding usage text without invoking the operator agent

#### Scenario: Existing management commands remain available
- **GIVEN** an authenticated operator uses existing `/jobs`, `/agents`, or `/stop` commands
- **WHEN** the new history/result subcommands are added
- **THEN** the existing command and cancellation contracts remain unchanged

#### Scenario: Help discovers the history and result views
- **GIVEN** an authenticated operator requests `/help` or views `/jobs` usage
- **WHEN** command help is presented
- **THEN** it documents `/jobs history <tag>` and `/jobs result <execution-id>` as read-only views
- **AND** it preserves the existing `/jobs` management command guidance

#### Scenario: Unauthenticated user cannot read job records
- **GIVEN** a Telegram user is not authenticated as the authorised operator
- **WHEN** that user requests `/jobs history <tag>` or `/jobs result <execution-id>`
- **THEN** access is denied under the existing authentication policy
- **AND** no retained job information is disclosed

### Requirement: `/models` lists, refreshes, and selects discovered models

Authorized `/models` SHALL list about 20 cached candidates, offer refresh of every configured provider, and show per-provider fresh/stale status and the active selection as saved or an unsaved automatic choice, even when not cached. Capabilities and limits SHALL be labelled advisory, not budgets or a callability promise. Refresh SHALL update only the cache. Selection SHALL occur only through candidate buttons with opaque callbacks of at most 64 bytes; `/models` arguments SHALL NOT select. The model registry SHALL be required at startup, with no registry-absent listing or mutation path, and a raw legacy `model:<name>` callback SHALL NOT select.

#### Scenario: `/models` presents a concise catalog
- **GIVEN** a cache contains about 20 candidates with fresh and stale providers
- **WHEN** an authorized user invokes `/models`
- **THEN** the list shows the candidates, provider freshness, and the active selection marked saved or automatic
- **AND** displayed metadata is labelled advisory
- **AND** no general pagination framework is introduced

#### Scenario: Uncached active selection is shown
- **GIVEN** the saved selection is not among the cached candidates
- **WHEN** an authorized user invokes `/models`
- **THEN** the selection is shown as active and saved without being marked unavailable

#### Scenario: Empty cache leaves commands usable
- **GIVEN** the cache has no candidates and every provider failed
- **WHEN** an authorized user invokes `/models`
- **THEN** the response states that no candidates are cached and offers refresh only; it names no typed selection command
- **AND** unrelated commands remain available

#### Scenario: Typed select text is not a selection route
- **GIVEN** a saved active selection with known preference bytes, or an automatic active selection with no preference file
- **WHEN** the authorized operator sends `/models select openai-prod/gpt-new`
- **THEN** the catalog is shown as for `/models` and no selection is made
- **AND** neither the preference (bytes or absence) nor the active selection change

#### Scenario: Corrupt preference with an empty cache recovers after a successful refresh
- **GIVEN** `model-selection.toml` is corrupt, the cache has no candidates, and no automatic selection was made
- **WHEN** the operator refreshes and a provider returns candidates, then selects one with its button
- **THEN** the corrupt preference is atomically replaced before the selection is published and active without a restart
- **AND** until then `/models` reports the corrupt preference without file content, offers refresh only, and refresh updates only the cache and never writes or repairs the preference

#### Scenario: Refresh failure preserves stale inventory
- **GIVEN** a provider refresh fails
- **WHEN** `/models` displays the catalog
- **THEN** its last-good candidates remain selectable and marked stale
- **AND** the failure does not disable unrelated commands or change the active selection

#### Scenario: Callback payload does not include model identity
- **GIVEN** a canonical model ID is longer than Telegram's callback limit
- **WHEN** the selection button is rendered
- **THEN** it carries only an opaque token of at most 64 bytes
- **AND** the token resolves to the selector server-side

#### Scenario: Expired or removed selection token is rejected
- **GIVEN** a selection callback token expired or is unknown
- **WHEN** the callback is used
- **THEN** active selection and preference are unchanged
- **AND** the operator is asked to reopen `/models`
- **AND** an unexpired token whose candidate was later removed from the cache is not rejected for that reason

#### Scenario: Stale legacy model-name callback is refused
- **GIVEN** an authenticated user sends a callback whose payload is the raw legacy form `model:<name>` rather than an opaque token
- **WHEN** the callback is handled
- **THEN** it is refused and the operator is asked to reopen `/models`
- **AND** the active selection and preference bytes are unchanged and nothing is mutated
- **AND** opaque-token validation and expiry rules are unchanged

#### Scenario: Selection persistence fails
- **GIVEN** a candidate button is chosen but the preference cannot be persisted
- **WHEN** selection is attempted
- **THEN** the old active selection remains
- **AND** prior preference bytes remain unchanged and no success is reported

#### Scenario: Selecting replaces a corrupt preference
- **GIVEN** `/models` reports the saved preference as corrupt
- **WHEN** the operator selects a candidate
- **THEN** the preference is atomically replaced and the selection is active without restart

#### Scenario: Button selection needs no main selection and no cache membership
- **GIVEN** a valid unexpired token captures a selector
- **WHEN** its candidate button is used, whether or not a main selection is active
- **THEN** selection SHALL validate only the captured selector's non-empty first-slash shape and configured connection, never cache membership or upstream existence

#### Scenario: Button selection persists before it is published or reported
- **GIVEN** a candidate button is chosen
- **WHEN** the selection succeeds
- **THEN** the preference SHALL be atomically persisted before the active selection is published or success is reported

#### Scenario: Corrupt preference is reported without content or automatic fallback
- **GIVEN** the saved preference is corrupt or invalid
- **WHEN** `/models` displays the catalog
- **THEN** it SHALL say so without showing file content and SHALL NOT fall back to an automatic selection
- **AND** a deliberate button selection SHALL replace the preference without a restart

#### Scenario: Saved selection stays active while the cache is empty
- **GIVEN** a valid saved selector and an empty cache
- **WHEN** `/models` displays the catalog
- **THEN** the saved selector remains active and displayed
- **AND** with no usable unexpired candidate token, new selection buttons wait until a refresh or provider recovery produces candidates
- **AND** an already-issued usable unexpired token can still select its captured selector even if a refresh left the cache empty

# approval-grants Specification

## Purpose

Define the single grant mechanism for standing operator consent over file operations: one grant type `(tool, dir-recursive)` with two lifetimes, sink-side tool vetoes, and prohibition immunity — replacing `auto_approve_tools`, per-message request grants, and zone-button trust.

## Requirements

### Requirement: One grant type with recursive directory scope

The `GrantLedger` (owned by the depth-0 `ConfirmationManager` — this is the single ledger; the headless `_coordinator` bridge delegates to it and is not a separate ledger) MUST store grants as `(tool, dir, lifetime, scope_owner)`. A grant for `dir` covers `dir` itself and every path recursively under it (`/data` ⇒ `/data/**`), matching policy containment semantics. A grant covers exactly one tool.

#### Scenario: Grant covers nested paths for the granted tool
- **GIVEN** a prompt-lifetime grant `(file_write, /data)`
- **WHEN** `file_write` is invoked with path `/data/sub/deep/file.txt`
- **THEN** the operation executes without a confirmation prompt

#### Scenario: Grant does not cover sibling directories
- **GIVEN** a prompt-lifetime grant `(file_write, /data/reports)`
- **WHEN** `file_write` is invoked with path `/data/other.txt`
- **THEN** a confirmation prompt is sent

#### Scenario: Grant does not cover other tools
- **GIVEN** a prompt-lifetime grant `(file_write, /data)`
- **WHEN** `file_read` is invoked with path `/data/file.txt`
- **THEN** a confirmation prompt is sent (the grant is per-tool)

### Requirement: Two grant lifetimes — prompt and session

Grants MUST carry exactly one lifetime: `prompt` (valid until the end of the current user-message cycle, cleared at `react_loop()` entry) or `session` (valid until the operator sends `/reset`, which clears both lifetimes). Grants MUST be stored in-memory only and are never persisted to disk.

#### Scenario: Prompt grant dies at the next user message
- **GIVEN** the operator pressed Confirm (prompt lifetime) for `(file_write, /data)` during the current message cycle
- **WHEN** the user sends a new message and the react loop starts
- **THEN** the grant is no longer active and access to `/data` prompts again

#### Scenario: Session grant survives until /reset
- **GIVEN** the operator pressed "Till /reset" (session lifetime) for `(file_write, /data)`
- **AND** two subsequent user messages complete
- **WHEN** the operator sends `/reset`
- **THEN** the grant is cleared and subsequent access to `/data` prompts again

#### Scenario: Grants are never persisted
- **GIVEN** session grants are active
- **WHEN** the agent process restarts
- **THEN** no grant state exists after restart (grants were in-memory only)

### Requirement: Confirmation prompts offer three buttons with grant semantics

For file-tool operations on UNRECOGNISED paths, the confirmation prompt MUST offer `[✅ Confirm]` (one-shot execution + prompt-lifetime grant for `(tool, parent-dir)`), `[✅✅ Till /reset]` (one-shot execution + session-lifetime grant), and `[❌ Deny]` (refuse; the agent receives a denial result and may re-plan). The staged operation MUST execute exactly the frozen args captured at staging time; the file is never read before the decision for `file_patch` (confirm-before-read preserved).

#### Scenario: Confirm grants the parent directory for the prompt cycle
- **GIVEN** the agent requests `file_write` to `/data/reports/q1.txt` and the prompt is shown
- **WHEN** the operator presses Confirm
- **THEN** the write executes
- **AND** a subsequent `file_write` to `/data/reports/q2.txt` in the same cycle proceeds without a prompt

#### Scenario: Deny returns a refusal the agent can act on
- **GIVEN** a confirmation prompt for `file_write` to `/data/x.txt`
- **WHEN** the operator presses Deny
- **THEN** the tool returns a denial result (not a crash)
- **AND** the agent may choose a different approach in its next step

### Requirement: shell and secret_get can never hold standing grants

The grant ledger check MUST veto standing grants for `shell` and `secret_get` (`may_hold_grant(tool) -> False`), enforced at the ledger sink (not only at the Telegram boundary, so crafted callbacks cannot bypass it). Their confirmation prompts MUST render Confirm/Deny buttons only — no "Till /reset" affordance (no dead buttons). Every `shell` and `secret_get` confirmation remains a per-operation decision.

#### Scenario: Shell prompt renders two buttons only
- **GIVEN** a shell command requires confirmation
- **WHEN** the prompt is rendered
- **THEN** the buttons are Confirm and Deny only
- **AND** no "Till /reset" button appears

#### Scenario: Crafted approve-all callback for shell is rejected at the sink
- **GIVEN** a malicious or replayed Telegram callback attempts to create a standing grant for `shell`
- **WHEN** the ledger processes it
- **THEN** the grant is refused because `may_hold_grant("shell")` is False
- **AND** no grant entry exists and the command does not execute

#### Scenario: secret_get always asks
- **GIVEN** `secret_get` has been confirmed ten times this session
- **WHEN** the agent calls `secret_get` an eleventh time
- **THEN** a confirmation prompt is sent for this call

### Requirement: Prohibited paths are immune to grants

Classification runs before the ledger check: a Tier 0 prohibited path MUST fail with a hard error regardless of any active grant, and a grant can never be created for, or consulted for, a prohibited path.

#### Scenario: Grant for an allowed dir does not unlock a prohibited subdir
- **GIVEN** a session grant `(file_read, /data)` is active
- **AND** `/data/keys` is in `prohibited_dirs`
- **WHEN** `file_read` is invoked with a path under `/data/keys`
- **THEN** the call fails with a `prohibited_path` hard error
- **AND** no prompt is rendered

### Requirement: Sub-agent grants are scoped to the sub-agent's run

The ledger MUST remain single and depth-0-owned. A prompt-lifetime grant arising from a **turn-owned child** file-operation confirmation MUST carry the child run identity and MUST expire when that child run completes; session-lifetime grants from such confirmations behave as normal session grants. The active **operator turn** may provide the existing confirmation channel to its own on-demand children under the existing path policy and grant rules. A **scheduled run** MUST NOT consult the interactive grant ledger or confirmation coordinator, regardless of whether an operator turn is active or whether a matching prompt- or session-lifetime grant exists. A scheduled operation that requires additional approval MUST be denied promptly without rendering a prompt or staging a token. Classification-before-grant ordering, prohibited-path immunity, and sink-side vetoes remain unchanged.

#### Scenario: Sub-agent prompt grant dies with the sub-agent
- **GIVEN** a turn-owned child's file-operation confirmation receives a prompt-lifetime Confirm
- **AND** the grant scope is annotated to that child run
- **WHEN** the child run completes
- **THEN** the grant is removed from the depth-0 ledger
- **AND** the operator's file operations to that directory prompt again unless independently authorised

#### Scenario: No upward privilege leak
- **GIVEN** an active operator turn has spawned a turn-owned child
- **AND** the child has a prompt-lifetime grant for `(file_write, /data)`
- **WHEN** the operator calls `file_write` on a path under `/data`
- **THEN** a confirmation prompt is sent because the child's scoped grant does not cover the operator

#### Scenario: Scheduled run never consults an existing grant
- **GIVEN** an operator turn is active and the depth-0 ledger contains a matching session grant
- **WHEN** a scheduled run attempts an operation requiring additional approval
- **THEN** the operation is denied without consulting the grant ledger
- **AND** no confirmation prompt or pending token is created

#### Scenario: Scheduled denial applies outside ordinary file operations
- **GIVEN** a scheduled run attempts a vision, MCP, or sensitive built-in operation that would require operator approval
- **WHEN** the approval boundary is reached
- **THEN** the operation is denied without consulting the interactive ledger or coordinator
- **AND** no confirmation prompt is rendered

## MODIFIED Requirements

### Requirement: Headless confirm bridge checks the shared approval set

The `_headless_confirm_bridge` SHALL check the shared `auto_approve_tools` set on the coordinator before prompting the operator for a sub-agent's sensitive file operation. If the tool name is in the set, the operation is auto-approved without a prompt.

Feature: Builtin tool execution
Rule: The shared `auto_approve_tools` set is owned by the `ConfirmationManager` (the coordinator). The `BuiltinExecutor` holds a `_coordinator` reference during a run, set at run start and set to `None` at run end. One grant covers the main agent and all sub-agents for the prompt.

#### Scenario: Sub-agent op auto-approved when tool is in the shared set
- **GIVEN** "file_read" is in the coordinator's `auto_approve_tools` for the current prompt
- **WHEN** a sub-agent attempts a sensitive `file_read`
- **THEN** the operation is auto-approved via `confirm(token)` without an operator prompt
- **AND** zone classification still ran first inside `execute()` — only the zone-triggered confirmation was auto-satisfied

#### Scenario: Sub-agent op prompts when tool is not in the shared set
- **GIVEN** "file_write" is not in the coordinator's `auto_approve_tools`
- **WHEN** a sub-agent attempts a sensitive `file_write`
- **THEN** the operator is prompted with an "Approve all file_write" button
- **AND** the sub-agent blocks until the operator responds or the confirmation times out

#### Scenario: Shared set is None after run end (fail-closed)
- **GIVEN** the prompt's run has ended and the executor's `_coordinator` reference is set to `None`
- **WHEN** an orphaned sub-agent attempts a sensitive `file_read`
- **THEN** the operation is blocked without prompting the operator because the coordinator is `None`
- **AND** the sub-agent receives a fail-closed error result indicating the confirmation bridge is unavailable

#### Scenario: Sub-agent approve-all is atomic
- **GIVEN** a sub-agent confirmation prompt is pending and the operator taps "Approve all file_write"
- **WHEN** the coordinator's `signal_headless_confirmation` is called with `approve_all=True` and `tool_name="file_write"`
- **THEN** the tool name is added to `auto_approve_tools` AND the confirmation event is signalled in a single method call
- **AND** no concurrent sub-agent thread can observe the approval set without the event already being set

## ADDED Requirements

### Requirement: All confirmation signaling flows through a single coordinator

The `ConfirmationManager` SHALL own the signaling (Event + result dict + token lifecycle) for all four confirmation flow types: tool confirmation at depth 0, tool confirmation at depth ≥ 1 (headless bridge), step extension, and LLM error retry. The `BuiltinExecutor` SHALL delegate headless confirmation wait/signaling to the coordinator via `request_headless_confirmation()` and `signal_headless_confirmation()`.

Feature: Builtin tool execution
Rule: The coordinator owns Event lifecycle and `auto_approve_tools`; the executor owns tool staging (`_pending`), zone context (`_zone_paths`, `_zone_trackers`), and execution (`confirm`/`cancel`). The coordinator is transport-agnostic — the Telegram prompt callback is passed at call time, not stored on the coordinator.

#### Scenario: Headless bridge delegates to coordinator
- **GIVEN** a sub-agent requests confirmation for a sensitive file operation and the tool is not in the approval set
- **WHEN** the headless confirm bridge calls `coordinator.request_headless_confirmation()`
- **THEN** the coordinator creates a `threading.Event`, stores it in its own `_headless_confirm_events` dict, calls the provided prompt function, and blocks on the event
- **AND** the executor's `_headless_confirm_events` and `_headless_confirm_results` dicts are not used

#### Scenario: Telegram callback signals through coordinator
- **GIVEN** a sub-agent confirmation is pending and the operator responds via Telegram
- **WHEN** the Telegram callback handler calls `coordinator.signal_headless_confirmation(token, approved)`
- **THEN** the coordinator atomically pops the event from its `_headless_confirm_events` dict, writes the result to its `_headless_confirm_results` dict, and sets the event
- **AND** no direct mutation of `auto_approve_tools` occurs outside the coordinator

#### Scenario: Headless confirmation timeout fails closed
- **GIVEN** a sub-agent confirmation is pending and the operator does not respond within the timeout
- **WHEN** the coordinator's `request_headless_confirmation()` times out
- **THEN** the coordinator cleans up its event and result entries
- **AND** the bridge returns a fail-closed error result without executing the tool

#### Scenario: Prompt function is passed at call time
- **GIVEN** the headless confirm bridge needs to prompt the operator
- **WHEN** the bridge calls `coordinator.request_headless_confirmation()`
- **THEN** the Telegram prompt function is passed as a parameter from the executor's stored `_subagent_confirm_prompt_fn`
- **AND** the coordinator does not store the prompt function as an attribute
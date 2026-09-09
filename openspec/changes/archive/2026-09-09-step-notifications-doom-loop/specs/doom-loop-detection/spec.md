## ADDED Requirements

### Requirement: Agent self-terminates on three identical consecutive tool failures

The agent SHALL self-terminate with a descriptive stop message when the same tool call fails with the same error three times consecutively. "Same" is determined by exact match on the composite key `f"{tool_name}:{error_text[:200]}"`, where `error_text` is `(outcome.get("error", "") or "").strip()` and `outcome.get("success") is False`. Self-termination MUST produce a user-readable return string from the react loop; it MUST NOT raise an exception.

#### Scenario: Three identical failures trigger self-termination
- **GIVEN** the agent calls tool `shell` with a command that fails with error "Permission denied"
- **WHEN** the same tool (`shell`) fails with the same error ("Permission denied", first 200 chars) three times in a row
- **THEN** the agent returns a descriptive stop message to the user (e.g., "❌ Agent stuck: same failure repeated 3× — stopping to avoid wasting budget")
- **AND** no further steps are taken

#### Scenario: Two identical failures then success does not trigger termination
- **GIVEN** `shell` has failed twice with the same error
- **WHEN** `shell` succeeds on the third call
- **THEN** the failure counter resets to zero
- **AND** the agent continues running normally

#### Scenario: Two identical failures then a different error resets the counter
- **GIVEN** `shell` has failed twice with error "Permission denied"
- **WHEN** `shell` fails with a different error ("File not found")
- **THEN** the counter resets and the new error becomes the tracked failure
- **AND** the agent is not terminated (only one consecutive failure of the new type)

#### Scenario: Same error from different tools does not trigger termination
- **GIVEN** `file_read` fails with error "File not found"
- **AND** `file_write` fails with error "File not found"
- **WHEN** these two different tools each fail once with the same error text
- **THEN** the counter does not accumulate across tool names
- **AND** the agent is not terminated

#### Scenario: Limit of exactly three — not two or four
- **GIVEN** the agent calls `file_write` and fails with the same error twice
- **WHEN** `file_write` fails with the same error a second time
- **THEN** the agent is NOT terminated (only 2 consecutive identical failures)
- **WHEN** `file_write` fails with the same error a third time
- **THEN** the agent IS terminated

### Requirement: Any successful tool call resets the doom-loop counter

After any tool call that returns `success: True` (or a result where `outcome.get("success") is not False`), the doom-loop failure key and repeat counter SHALL be reset to their initial state (`""` and `0` respectively).

#### Scenario: Success after failures clears state
- **GIVEN** the agent has accumulated 2 consecutive identical failures for `shell`
- **WHEN** any tool call (same or different tool) returns `success: True`
- **THEN** `_last_tool_fail_key` is reset to `""`
- **AND** `_tool_fail_repeat` is reset to `0`
- **AND** subsequent failures start a fresh counter

### Requirement: Doom-loop detection applies to all agent types

The self-termination check SHALL run in the react loop for the main agent, scheduled sub-agents, plan-step sub-agents, and on-demand sub-agents. No agent type is exempt.

#### Scenario: Scheduled job self-terminates on doom-loop
- **GIVEN** a scheduled job is running
- **WHEN** the same tool call fails with the same error three consecutive times
- **THEN** the scheduled job's react loop returns the stop message
- **AND** the scheduler records the job as failed with that message

### Requirement: Doom-loop limit is configurable via module constant

The consecutive-failure threshold SHALL be defined as a module-level constant `_DOOM_LOOP_LIMIT = 3` in `react_loop.py`, following the same convention as `_JSON_FAIL_LIMIT`.

#### Scenario: Constant controls the threshold
- **GIVEN** `_DOOM_LOOP_LIMIT = 3`
- **WHEN** identical failures accumulate
- **THEN** self-termination occurs at the third failure, not before

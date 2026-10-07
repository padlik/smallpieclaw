# Sub-Agent Council Control Specification

## Purpose

Define the `wait_for_any_agent` and `cancel_agent` built-in tools that enable the council pattern: the main agent can consume sub-agent results in completion order and cancel sub-agents it no longer needs.

## Requirements

### Requirement: wait_for_any_agent returns first completed own child

The system SHALL provide `wait_for_any_agent`, returning the first completed result of the caller's own on-demand children in the current **operator turn**. Failed or cancelled children count as completed; a discarded result yields cancelled/discarded status, not its text.

Feature: Sub-agent council control
Rule: The model-facing result consumer is the operator turn that admitted the child; each turn consumes only its own results.

#### Scenario: First completed sub-agent is returned
- **GIVEN** own-turn children A, B, and C are running in parallel and B finishes first
- **WHEN** the operator turn calls `wait_for_any_agent` with `agent_ids=[A, B, C]`
- **THEN** it returns B's `agent_id`, result, and status
- **AND** A and C continue running unaffected

#### Scenario: Already-finished agents return immediately
- **GIVEN** own-turn child B has finished while A is still running
- **WHEN** the operator turn calls `wait_for_any_agent` with `agent_ids=[A, B]`
- **THEN** it returns B's result immediately without waiting

#### Scenario: Failed or cancelled agents are returned as completed
- **GIVEN** own-turn child A has terminal status `"failed"` or `"cancelled"` and its result was not explicitly discarded
- **WHEN** the operator turn calls `wait_for_any_agent` with A and another own-turn child B
- **THEN** A is returned immediately with its terminal status and result
- **AND** the operator turn can decide whether to proceed or cancel B

#### Scenario: Discarded result immediately returns cancelled status
- **GIVEN** the caller explicitly cancelled a completed but unread own-turn child
- **WHEN** it later requests that child through `get_agent_result` or `wait_for_any_agent`
- **THEN** the tool immediately reports an actionable cancelled/discarded status
- **AND** it does not return the original result text

### Requirement: Non-owned wait input is rejected without disclosure

A scheduled, other-turn, or unknown ID, or a list mixing one with own children, SHALL return the same error naming the supplied ID and stating that no child is accessible in this turn, without waiting or revealing whether a non-owned run exists. A timeout SHALL NOT cancel children.

#### Scenario: Mixed wait input is rejected as a whole
- **GIVEN** the caller supplies an ID list containing an own-turn child and a scheduled, other-turn, or unknown ID
- **WHEN** it calls `wait_for_any_agent`
- **THEN** the tool returns an error naming the supplied ID and saying no child is accessible in this turn
- **AND** it does not wait on any ID or disclose whether the non-owned run exists

#### Scenario: Timeout returns no result
- **GIVEN** none of the specified own-turn children finish before the timeout
- **WHEN** the caller invokes `wait_for_any_agent`
- **THEN** it returns `status="timeout"` and the still-pending own-turn IDs
- **AND** no child is cancelled

#### Scenario: Unknown agent ID is rejected
- **GIVEN** the caller supplies an unknown agent ID
- **WHEN** the operator turn calls `wait_for_any_agent` with that ID
- **THEN** the tool returns an error naming the supplied ID and stating no child is accessible in this turn
- **AND** it does not wait or disclose whether such a run exists

### Requirement: cancel_agent targets the caller's own on-demand children

The system SHALL provide `cancel_agent` without confirmation, targeting only on-demand children owned by the caller's current **operator turn**. Cancelling a completed but unread own child SHALL discard its result and account for it toward turn completion.

Feature: Sub-agent council control
Rule: Model cancellation is limited to the caller's own admitted workers; operator controls remain cross-source.

#### Scenario: Cancel a specific sub-agent
- **GIVEN** own-turn child A is running and no longer needed
- **WHEN** the operator turn calls `cancel_agent` with A's `agent_id`
- **THEN** A's cancel event is set and its in-flight LLM request is interrupted
- **AND** its run terminates with `status="cancelled"`

#### Scenario: Cancelling completed child discards unread result
- **GIVEN** own-turn child A completed but its result was not retrieved
- **WHEN** the operator turn calls `cancel_agent` for A
- **THEN** A is marked accounted for and its unread result is discarded
- **AND** later result-tool calls return cancelled/discarded status without its result text

#### Scenario: Cancel is not confirmation-gated
- **GIVEN** an own-turn child is running, including while it has a pending confirmation
- **WHEN** the operator turn calls `cancel_agent` for that child
- **THEN** no additional operator confirmation prompt is sent
- **AND** cancellation proceeds immediately

#### Scenario: Cancellation during confirmation keeps turn draining
- **GIVEN** an own-turn child is awaiting confirmation through the active operator turn
- **WHEN** the child is cancelled
- **THEN** cancellation proceeds without an additional confirmation prompt
- **AND** the operator turn remains active until the child's lifecycle cleanup completes

### Requirement: Model cancellation does not disclose or affect other runs

A specific scheduled, other-turn, or unknown ID SHALL receive the same non-disclosing inaccessible-child error used by result tools. The `managed` and `all` aliases SHALL always succeed, returning the count of the caller's own children newly cancelled or discarded (zero when none), and SHALL NOT count or disclose other runs. The operator retains cross-source `/agents` and broad `/stop` controls.

#### Scenario: Cancel all managed sub-agents
- **GIVEN** own-turn children A and B are cancellable and scheduled run S is active
- **WHEN** the operator turn calls `cancel_agent` with `agent_id="managed"` or `"all"`
- **THEN** A and B are cancelled and the count reflects only own children newly cancelled or discarded
- **AND** S remains running and is not counted or disclosed
- **AND** the alias succeeds with count zero if no own child is newly cancelled or discarded

#### Scenario: Non-owned specific cancellation does not disclose a run
- **GIVEN** the caller supplies a scheduled, other-turn, or unknown ID
- **WHEN** it calls `cancel_agent` with that ID
- **THEN** it receives the uniform inaccessible-child error naming the supplied ID
- **AND** no other run is cancelled or disclosed

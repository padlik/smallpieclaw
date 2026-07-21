## ADDED Requirements

### Requirement: Main agent can wait for any of a set of sub-agents

The system SHALL provide a `wait_for_any_agent` built-in tool that blocks until one of the specified sub-agents finishes and returns that agent's result, letting the main agent consume results in completion order.

Feature: Sub-agent council control
Rule: The main agent calls `wait_for_any_agent` repeatedly to collect results as they arrive, deciding after each whether it has enough — the council pattern.

#### Scenario: First completed sub-agent is returned
- **GIVEN** sub-agents A, B, and C are running in parallel and B finishes first
- **WHEN** the main agent calls `wait_for_any_agent` with `agent_ids=[A, B, C]`
- **THEN** the tool returns B's `agent_id`, result, and status
- **AND** A and C continue running unaffected

#### Scenario: Already-finished agents return immediately
- **GIVEN** sub-agent B has already finished and A is still running
- **WHEN** the main agent calls `wait_for_any_agent` with `agent_ids=[A, B]`
- **THEN** the tool returns B's result immediately without waiting

#### Scenario: Failed or cancelled agents are returned as completed
- **GIVEN** sub-agent A has crashed and its status is `"failed"`
- **WHEN** the main agent calls `wait_for_any_agent` with `agent_ids=[A, B]`
- **THEN** the tool returns A's `agent_id`, result (the error), and `status="failed"`
- **AND** the main agent can decide whether to proceed or cancel B

#### Scenario: Timeout returns no result
- **GIVEN** none of the specified sub-agents finish within the timeout
- **WHEN** the main agent calls `wait_for_any_agent` with a timeout
- **THEN** the tool returns `status="timeout"` with the list of still-pending `agent_ids`
- **AND** no sub-agents are cancelled by the timeout

#### Scenario: Unknown agent ID is rejected
- **GIVEN** the main agent calls `wait_for_any_agent` with an `agent_id` that is not in the registry
- **WHEN** the tool executes
- **THEN** an error result is returned indicating the unknown agent ID
- **AND** no wait occurs

### Requirement: Main agent can cancel its own sub-agents

The system SHALL provide a `cancel_agent` built-in tool that lets the main agent cancel a specific sub-agent or all managed sub-agents, without operator confirmation.

Feature: Sub-agent council control
Rule: The LLM cancelling its own spawned workers is analogous to the existing `get_agent_result` timeout-cancel — not confirmation-gated. The operator retains `/agents cancel` and `/stop` as overrides.

#### Scenario: Cancel a specific sub-agent
- **GIVEN** sub-agent A is running and the main agent no longer needs its result
- **WHEN** the main agent calls `cancel_agent` with A's `agent_id`
- **THEN** A's cancel event is set and its in-flight LLM request is interrupted
- **AND** A's run terminates with `status="cancelled"`

#### Scenario: Cancel all managed sub-agents
- **GIVEN** sub-agents A (on-demand) and B (on-demand) are running
- **WHEN** the main agent calls `cancel_agent` with `agent_id="managed"`
- **THEN** both A and B are cancelled
- **AND** the tool returns the count of cancelled sub-agents

#### Scenario: Cancel is not confirmation-gated
- **GIVEN** the main agent calls `cancel_agent` for a running sub-agent
- **WHEN** the tool executes
- **THEN** no operator confirmation prompt is sent
- **AND** the cancellation proceeds immediately
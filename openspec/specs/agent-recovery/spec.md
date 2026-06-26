# agent-recovery Specification

## Purpose
Define structured tool errors and recovery behavior for agent execution.

## Requirements

### Requirement: Structured error fields
All tool outcomes SHALL include three new fields: `error_type` (string, kebab-case), `recoverable` (boolean), and `suggestion` (string, optional). These are populated by built-in tools and propagated by the ReAct loop.

#### Scenario: Shell timeout returns structured error
- **WHEN** a shell command times out after 10 seconds
- **THEN** the outcome includes `error_type: "tool_timeout"`, `recoverable: true`, `suggestion: "Try with timeout=30"`

### Requirement: Simple recovery for known transient errors
The executor SHALL automatically retry steps with `recoverable: true` and known `error_type` values (`tool_timeout`, `syntax_error`, `network_error`), up to 2 retries per step, with exponential backoff. Retries use the same tool and args.

#### Scenario: Automatic retry on timeout
- **WHEN** a step fails with `error_type: "tool_timeout"`
- **THEN** the executor waits 2s, retries the same step, and if it succeeds, continues execution

### Requirement: Complex recovery spawns diagnostic sub-agent
If a step fails with `recoverable: false` or exhausts retries, the executor SHALL spawn a diagnostic sub-agent with the task: "Analyze why this tool failed and suggest an alternative approach." The diagnostic result is fed back to the parent agent, which may emit a revised plan.

#### Scenario: Diagnostic re-planning
- **WHEN** a step fails with `recoverable: false` and `error_type: "permission_denied"`
- **THEN** a diagnostic sub-agent analyzes the failure, suggests using `sudo` or an alternative tool, and the parent re-plans

### Requirement: Recovery never retries planning failures
The executor SHALL NOT automatically retry steps where `error_type` indicates a planning failure (`wrong_model_for_task`, `fundamentally_wrong_approach`, `impossible_with_current_tools`). These are immediately escalated to Complex recovery.

#### Scenario: Wrong model selected
- **WHEN** a vision task is assigned to a non-multimodal model
- **THEN** the error `error_type: "wrong_model_for_task"` is escalated immediately — no automatic retry

### Requirement: Error type registry
The system SHALL maintain a registry of known `error_type` values, each mapped to: default `recoverable` flag, retry strategy (count, backoff), and whether it requires Complex recovery. Custom tools may register their own error types.

#### Scenario: Custom tool registers error type
- **WHEN** a new tool calls `register_error_type("api_rate_limited", recoverable=true, max_retries=3, backoff="linear")`
- **THEN** the executor applies this retry policy for that error type

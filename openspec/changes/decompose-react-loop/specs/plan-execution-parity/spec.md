## ADDED Requirements

### Requirement: Plan execution emits tool trace and working-memory step regardless of dispatch path

Feature: Plan execution observability
Rule: When the agent executes a plan action, it must emit a tool trace event and record a working-memory step regardless of whether the LLM used native tool calls or json_mode text output to express the plan.

#### Scenario: json_mode plan emits tool trace
- **GIVEN** the agent is running in json_mode (no native tool calls)
- **WHEN** the LLM returns a `plan` action object
- **THEN** `on_tool_trace` is called with a `ToolTrace` for the plan action
- **AND** the trace records the plan steps and outcome

#### Scenario: json_mode plan records working-memory step
- **GIVEN** the agent is running in json_mode (no native tool calls)
- **WHEN** the LLM returns a `plan` action object
- **THEN** `working.add_step("plan", …)` is called with the plan description
- **AND** the working memory reflects the plan execution

#### Scenario: native plan emits tool trace (existing behaviour preserved)
- **GIVEN** the agent is running with native tool calls enabled
- **WHEN** the LLM returns a native `plan` tool call
- **THEN** `on_tool_trace` is called with a `ToolTrace` for the plan action
- **AND** `working.add_step("plan", …)` is called

#### Scenario: plan ceiling is single-sourced
- **GIVEN** a plan is being executed
- **WHEN** the number of plan steps would exceed the absolute ceiling
- **THEN** the plan is capped at `ABSOLUTE_PLAN_CEILING` steps
- **AND** this ceiling is defined once in the codebase (not duplicated)

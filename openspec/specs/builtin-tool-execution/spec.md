# builtin-tool-execution Specification

## Purpose
Define the built-in tool execution framework: name-based dispatch, two-phase confirmation gating for dangerous/sensitive tools, and the enumeration contract for built-in tools (including `vision_query`, which is executed by the ReAct loop rather than executor dispatch).

## Requirements

### Requirement: Built-in tool dispatch is total and deterministic

The built-in executor MUST route every built-in tool call by tool name to exactly one
handler, and MUST return an error result (never raise) for an unknown name. This capability
governs the dispatch and confirmation *framework* only; the semantics of any individual tool
are owned by that tool's own capability (for example `secret_get` by `vault-runtime-lookup`,
`log_query` by `runtime-log-introspection`, and structured error fields by `agent-recovery`)
and are unchanged by this behaviour.

#### Scenario: A known built-in routes to its handler
- **GIVEN** the built-in executor
- **WHEN** a caller invokes `execute` with a known built-in tool name and arguments
- **THEN** the call is routed to that tool's handler
- **AND** the returned result is that handler's own result, to which dispatch adds no transformation

#### Scenario: An unknown built-in returns an error result
- **GIVEN** the built-in executor
- **WHEN** a caller invokes `execute` with a tool name that is not a built-in
- **THEN** a result with `success = False` and a non-empty `error` is returned
- **AND** no exception is raised

#### Scenario: The built-in set is enumerable and stable
- **GIVEN** the built-in executor
- **WHEN** a caller queries `is_builtin` for a name or lists `all_tools`
- **THEN** the same fixed set of 15 built-in tools is enumerated by `is_builtin` and `all_tools`

### Requirement: Dangerous and sensitive tools gate through two-phase confirmation

A confirmation-capable built-in MUST NOT perform its effect until confirmed. In
interactive mode the first phase MUST stage a token and request confirmation; the second phase
MUST execute only on approval. This framework behaviour is preserved across the module split; the
specific condition that makes a given tool confirmation-capable is owned by that tool's
capability.

#### Scenario: Interactive confirmation is staged before any effect
- **GIVEN** a caller at interactive depth invokes a confirmation-capable built-in (for example a dangerous `file_write`)
- **WHEN** `execute` is called
- **THEN** the result indicates `requires_confirmation = True` with a confirmation token
- **AND** the tool's effect has not yet been performed

#### Scenario: Confirming a staged token performs the effect
- **GIVEN** a staged confirmation token from a prior `execute`
- **WHEN** the caller calls `confirm` with that token
- **THEN** the staged operation executes and returns its normal result

#### Scenario: Cancelling a staged token performs no effect
- **GIVEN** a staged confirmation token from a prior `execute`
- **WHEN** the caller calls `cancel` with that token
- **THEN** the staged operation is discarded and its effect is never performed

#### Scenario: Headless sub-agent sensitive operations bridge to the operator
- **GIVEN** a sub-agent (headless caller) invokes a sensitive confirmation-capable built-in
- **WHEN** the operation would require confirmation
- **THEN** the operator is prompted out of band and the sub-agent blocks on the response
- **AND** the operation executes only if the operator approves, and is blocked on denial or timeout

#### Scenario: The set of confirmation-capable built-ins is fixed
- **GIVEN** the built-in executor
- **WHEN** the built-ins that gate through confirmation are enumerated
- **THEN** exactly six built-ins gate: `shell`, `file_read`, `file_write`, `file_patch`, `memory_graph_store`, and `secret_get`

### Requirement: `vision_query` is a declared built-in executed by the ReAct loop

`vision_query` MUST be enumerated among the built-in tools but MUST be executed by the
ReAct loop rather than by the built-in executor's dispatch, because it needs LLM access the
executor does not hold. The built-in executor's dispatch MUST NOT hold a handler for it.

#### Scenario: vision_query is enumerated as a built-in
- **GIVEN** the built-in executor
- **WHEN** a caller queries `is_builtin("vision_query")` or lists `all_tools`
- **THEN** `vision_query` is reported as a built-in

#### Scenario: vision_query is executed by the ReAct loop, not executor dispatch
- **GIVEN** the ReAct loop processing a `vision_query` tool call
- **WHEN** the tool is handled
- **THEN** the ReAct loop performs the vision query directly
- **AND** invoking `execute` with `vision_query` on the built-in executor does not itself perform a vision query

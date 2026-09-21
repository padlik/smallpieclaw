## MODIFIED Requirements

### Requirement: Built-in tool dispatch is total and deterministic

The built-in executor MUST route every built-in tool call by tool name to exactly one
handler, and MUST return an error result (never raise) for an unknown name. This capability
governs the dispatch and confirmation *framework* only; the semantics of any individual tool
are owned by that tool's own capability (for example `secret_get` by `vault-runtime-lookup`,
`log_query` and `log_detail` by `runtime-log-introspection`,
`wait_for_any_agent` and `cancel_agent` by `sub-agent-council-control`, `context_profile` by
`context-monitoring`,
and structured error fields by `agent-recovery`)
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
- **THEN** the same fixed set of 23 built-in tools is enumerated by `is_builtin` and `all_tools`

#### Scenario: Dispatch result is independent of which internal module implements a handler
- **GIVEN** the built-in executor
- **WHEN** a caller invokes `execute` for a known built-in tool
- **THEN** the result depends only on the tool name and arguments, never on which
  internal module or function holds that handler's implementation
- **AND** a caller cannot distinguish, from `execute`'s result or from
  `is_builtin`/`all_tools` enumeration, which module implements a given handler

## ADDED Requirements

### Requirement: log_detail is a registered built-in tool

The built-in executor SHALL register `log_detail` as a built-in tool, routed by name to its handler in `builtin_tools/secrets_log.py` (beside the `log_query` handler). The tool returns one full log record by store `id`; its read semantics are owned by `runtime-log-introspection`. It is not confirmation-capable.

Feature: Builtin tool execution
Rule: The built-in set grows from 22 to 23 tools. The confirmation-capable set stays at 8 (shell, file_read, file_write, file_patch, file_diff, file_send, memory_graph_store, secret_get) — the new tool is not confirmation-capable.

#### Scenario: log_detail is enumerated as a built-in
- **GIVEN** the built-in executor
- **WHEN** a caller queries `is_builtin("log_detail")` or lists `all_tools`
- **THEN** `log_detail` is reported as a built-in

#### Scenario: log_detail is not confirmation-capable
- **GIVEN** the built-in executor
- **WHEN** the confirmation-capable built-ins are enumerated
- **THEN** exactly eight built-ins gate: `shell`, `file_read`, `file_write`, `file_patch`, `file_diff`, `file_send`, `memory_graph_store`, and `secret_get`
- **AND** `log_detail` is not in the confirmation-capable set
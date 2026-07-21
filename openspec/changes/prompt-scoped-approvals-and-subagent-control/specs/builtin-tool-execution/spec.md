## ADDED Requirements

### Requirement: Approval grants expire at run end

`auto_approve_tools` SHALL be cleared at the end of `AgentController.run()`, so approval grants expire when the prompt's results are presented. Grants do not survive into the next prompt.

Feature: Builtin tool execution
Rule: Per-prompt TTL — the operator's mental model is "I approved this for the current task," not "I approved this forever."

#### Scenario: Grant expires when the run ends
- **GIVEN** the operator tapped "Approve all file_read" during prompt #7
- **WHEN** prompt #7's run ends (results presented or error)
- **THEN** `auto_approve_tools` is cleared
- **AND** prompt #8 starts with an empty approval set — no grants carry over

#### Scenario: /reset still clears the approval set
- **GIVEN** the operator tapped "Approve all file_write" during the current prompt
- **WHEN** the operator runs `/reset` before the run ends
- **THEN** `auto_approve_tools` is cleared
- **AND** subsequent sensitive `file_write` calls in the same run re-prompt the operator

### Requirement: Headless confirm bridge checks the shared approval set

The `_headless_confirm_bridge` SHALL check the shared `_prompt_approval_set` on the `BuiltinExecutor` before prompting the operator for a sub-agent's sensitive file operation. If the tool name is in the set, the operation is auto-approved without a prompt.

Feature: Builtin tool execution
Rule: The shared set is the same `auto_approve_tools` object owned by the main agent's `ConfirmationManager` — one grant covers the main agent and all sub-agents for the prompt.

#### Scenario: Sub-agent op auto-approved when tool is in the shared set
- **GIVEN** "file_read" is in the shared `_prompt_approval_set` for the current prompt
- **WHEN** a sub-agent attempts a sensitive `file_read`
- **THEN** the operation is auto-approved via `confirm(token)` without an operator prompt
- **AND** zone classification still ran first inside `execute()` — only the zone-triggered confirmation was auto-satisfied

#### Scenario: Sub-agent op prompts when tool is not in the shared set
- **GIVEN** "file_write" is not in the shared `_prompt_approval_set`
- **WHEN** a sub-agent attempts a sensitive `file_write`
- **THEN** the operator is prompted with an "Approve all file_write" button
- **AND** the sub-agent blocks until the operator responds or the confirmation times out

#### Scenario: Shared set is None after run end (fail-closed)
- **GIVEN** the prompt's run has ended and `_prompt_approval_set` is set to `None`
- **WHEN** an orphaned sub-agent attempts a sensitive `file_read`
- **THEN** the operator is prompted (fail-closed) because the set is `None`, not empty

### Requirement: wait_for_any_agent and cancel_agent are registered built-ins

The built-in executor SHALL register `wait_for_any_agent` and `cancel_agent` as built-in tools, routed by name to their handlers in `builtin_tools/agents.py`. They are not confirmation-capable.

Feature: Builtin tool execution
Rule: The built-in set grows from 15 to 17 tools. The confirmation-capable set stays at 6 (shell, file_read, file_write, file_patch, memory_graph_store, secret_get) — the new tools are not confirmation-capable.

#### Scenario: wait_for_any_agent is enumerated as a built-in
- **GIVEN** the built-in executor
- **WHEN** a caller queries `is_builtin("wait_for_any_agent")` or lists `all_tools`
- **THEN** `wait_for_any_agent` is reported as a built-in

#### Scenario: cancel_agent is enumerated as a built-in
- **GIVEN** the built-in executor
- **WHEN** a caller queries `is_builtin("cancel_agent")` or lists `all_tools`
- **THEN** `cancel_agent` is reported as a built-in

#### Scenario: New tools are not confirmation-capable
- **GIVEN** the built-in executor
- **WHEN** the confirmation-capable built-ins are enumerated
- **THEN** exactly six built-ins gate: `shell`, `file_read`, `file_write`, `file_patch`, `memory_graph_store`, and `secret_get`
- **AND** `wait_for_any_agent` and `cancel_agent` are not in the confirmation-capable set

#### Scenario: shell cannot enter the shared approval set
- **GIVEN** the operator is offered approve-all buttons only for file tools (`file_read`, `file_write`, `file_patch`)
- **WHEN** the operator grants an approve-all during a prompt
- **THEN** `shell` is never added to `auto_approve_tools`
- **AND** `shell` remains always-confirmed for the main agent and always-blocked for sub-agents, regardless of any approve-all grant

## MODIFIED Requirements

### Requirement: Built-in tool dispatch is total and deterministic

The built-in executor MUST route every built-in tool call by tool name to exactly one
handler, and MUST return an error result (never raise) for an unknown name. This capability
governs the dispatch and confirmation *framework* only; the semantics of any individual tool
are owned by that tool's own capability (for example `secret_get` by `vault-runtime-lookup`,
`log_query` by `runtime-log-introspection`, `wait_for_any_agent` and `cancel_agent` by
`sub-agent-council-control`, and structured error fields by `agent-recovery`)
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
- **THEN** the same fixed set of 17 built-in tools is enumerated by `is_builtin` and `all_tools`

#### Scenario: Dispatch result is independent of which internal module implements a handler
- **GIVEN** the built-in executor
- **WHEN** a caller invokes `execute` for a known built-in tool
- **THEN** the result depends only on the tool name and arguments, never on which
  internal module or function holds that handler's implementation
- **AND** a caller cannot distinguish, from `execute`'s result or from
  `is_builtin`/`all_tools` enumeration, which module implements a given handler
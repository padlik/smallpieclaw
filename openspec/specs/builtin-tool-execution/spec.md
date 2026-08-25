# builtin-tool-execution Specification

## Purpose
Define the built-in tool execution framework: name-based dispatch, two-phase confirmation gating for dangerous/sensitive tools, and the enumeration contract for built-in tools (including `vision_query`, which is executed by the ReAct loop rather than executor dispatch).

## Requirements

### Requirement: Built-in tool dispatch is total and deterministic

The built-in executor MUST route every built-in tool call by tool name to exactly one
handler, and MUST return an error result (never raise) for an unknown name. This capability
governs the dispatch and confirmation *framework* only; the semantics of any individual tool
are owned by that tool's own capability (for example `secret_get` by `vault-runtime-lookup`,
`log_query` by `runtime-log-introspection`, `wait_for_any_agent` and `cancel_agent` by
`sub-agent-council-control`, `context_profile` by `context-monitoring`,
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
- **THEN** the same fixed set of 22 built-in tools is enumerated by `is_builtin` and `all_tools`

#### Scenario: Dispatch result is independent of which internal module implements a handler
- **GIVEN** the built-in executor
- **WHEN** a caller invokes `execute` for a known built-in tool
- **THEN** the result depends only on the tool name and arguments, never on which
  internal module or function holds that handler's implementation
- **AND** a caller cannot distinguish, from `execute`'s result or from
  `is_builtin`/`all_tools` enumeration, which module implements a given handler

### Requirement: Dangerous and sensitive tools gate through two-phase confirmation

A confirmation-capable built-in MUST NOT perform its effect until confirmed. In
interactive mode the first phase MUST stage a token and request confirmation; the second phase
MUST execute only on approval. This framework behaviour is preserved across the module split; the
specific condition that makes a given tool confirmation-capable is owned by that tool's
capability.

When the nsjail shell backend is active, the shell tool's confirmation gate becomes configurable
via `shell_nsjail_confirm_mode`. The dangerous-pattern detection (`_is_dangerous_shell`) returns
a 3-tuple `(dangerous, reason, category)` where category is one of: `host_escape`, `network`,
`resource`, `project`, `policy`. The `_should_confirm` method uses the category and the confirm
mode to decide whether to stage confirmation.

- `"always"` (default): confirm all dangerous patterns regardless of category. Backward-compatible.
- `"adaptive"`: skip confirmation for `network` category patterns when network isolation is
  active (`shell_nsjail_network == "none"`). `resource` category always confirms because
  `rlimit_nproc` is user-wide, not per-jail. All other categories still confirm.
- `"never"`: skip confirmation for all dangerous patterns when nsjail is active.

When nsjail is NOT active (fallback to subprocess), the confirm mode MUST behave as `"always"`
regardless of the config setting — the sandbox is not present, so all dangerous patterns must
confirm.

The configurable gate applies only at depth 0 (main agent). Sub-agents (caller_depth >= 1) already
fail closed for shell commands and are unchanged by this configuration.

This amends ADR-0011's invariant that "shell is never auto-approved for the main agent" — the
`"adaptive"` and `"never"` modes allow the operator to relax shell confirmation when nsjail
sandboxing is active. The sub-agent fail-closed half of ADR-0011 is preserved.

Feature: Builtin tool execution
Rule: The confirmation gate for shell is configurable when nsjail is active. The operator chooses the trust level.

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
- **THEN** exactly eight built-ins gate: `shell`, `file_read`, `file_write`, `file_patch`, `file_diff`, `file_send`, `memory_graph_store`, and `secret_get`

#### Scenario: always mode confirms all dangerous shell patterns
- **GIVEN** `shell_nsjail_confirm_mode` is `"always"` and nsjail is active
- **WHEN** the agent calls `shell("rm -rf build/")` (category: `project`)
- **THEN** the result indicates `requires_confirmation = True` with a confirmation token

#### Scenario: adaptive mode skips resource patterns
- **GIVEN** `shell_nsjail_confirm_mode` is `"adaptive"` and nsjail is active
- **WHEN** the agent calls `shell(":(){ :|:& };:")` (category: `resource`)
- **THEN** the command executes without confirmation (kernel-bounded by cgroup `pids_max`)

#### Scenario: adaptive mode still confirms project patterns
- **GIVEN** `shell_nsjail_confirm_mode` is `"adaptive"` and nsjail is active
- **WHEN** the agent calls `shell("rm -rf src/")` (category: `project`)
- **THEN** the result indicates `requires_confirmation = True`

#### Scenario: never mode skips all dangerous patterns when nsjail is active
- **GIVEN** `shell_nsjail_confirm_mode` is `"never"` and nsjail is active
- **WHEN** the agent calls `shell("rm -rf /")` (category: `host_escape`)
- **THEN** the command executes without confirmation
- **AND** the command may destroy files under RW-mounted host directories (project dir, RW trusted dirs) because the jail does not protect RW-mounted host paths
- **AND** read-only system mounts (/usr, /bin, /lib) are not affected

#### Scenario: never mode allows project-category destruction of host files
- **GIVEN** `shell_nsjail_confirm_mode` is `"never"` and nsjail is active
- **AND** the project directory is bind-mounted RW at its original host path
- **WHEN** the agent calls `shell("rm -rf /home/user/projects/myproject/src/")` (category: `project`)
- **THEN** the command executes without confirmation
- **AND** the files under `/home/user/projects/myproject/src/` are deleted on the host filesystem
- **AND** the jail does not prevent this because the project is a RW bind mount

#### Scenario: confirm mode falls back to always when nsjail is not active
- **GIVEN** `shell_nsjail_confirm_mode` is `"never"` but nsjail binary is not found (subprocess fallback)
- **WHEN** the agent calls `shell("rm -rf /")` (category: `host_escape`)
- **THEN** the result indicates `requires_confirmation = True` (always mode is used)

#### Scenario: Sub-agents fail closed regardless of confirm mode
- **GIVEN** `shell_nsjail_confirm_mode` is `"never"` and nsjail is active
- **AND** a sub-agent (caller_depth >= 1) calls `shell("rm -rf build/")`
- **WHEN** the shell tool processes the call
- **THEN** the command is blocked (sub-agents fail closed for shell)
- **AND** the confirm mode setting does not affect sub-agent behavior

### Requirement: The built-in tool set includes shell env management tools

The built-in executor MUST register `shell_env_set`, `shell_env_unset`, `shell_env_list`, and
`shell_env_get` as built-in tools, routed by name to their handlers in `builtin_tools/shell_env.py`.
They are not confirmation-capable.

Feature: Builtin tool execution
Rule: The built-in set grows from 17 to 21 tools. The confirmation-capable set stays at 8.

#### Scenario: shell_env_set is enumerated as a built-in
- **GIVEN** the built-in executor
- **WHEN** a caller queries `is_builtin("shell_env_set")` or lists `all_tools`
- **THEN** `shell_env_set` is reported as a built-in

#### Scenario: shell_env_unset is enumerated as a built-in
- **GIVEN** the built-in executor
- **WHEN** a caller queries `is_builtin("shell_env_unset")` or lists `all_tools`
- **THEN** `shell_env_unset` is reported as a built-in

#### Scenario: shell_env_list is enumerated as a built-in
- **GIVEN** the built-in executor
- **WHEN** a caller queries `is_builtin("shell_env_list")` or lists `all_tools`
- **THEN** `shell_env_list` is reported as a built-in

#### Scenario: shell_env_get is enumerated as a built-in
- **GIVEN** the built-in executor
- **WHEN** a caller queries `is_builtin("shell_env_get")` or lists `all_tools`
- **THEN** `shell_env_get` is reported as a built-in

#### Scenario: shell env tools are not confirmation-capable
- **GIVEN** the built-in executor
- **WHEN** the confirmation-capable built-ins are enumerated
- **THEN** exactly eight built-ins gate: `shell`, `file_read`, `file_write`, `file_patch`, `file_diff`, `file_send`, `memory_graph_store`, and `secret_get`
- **AND** `shell_env_set`, `shell_env_unset`, `shell_env_list`, and `shell_env_get` are not in the confirmation-capable set

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
Rule: The built-in set grows from 15 to 17 tools. The confirmation-capable set stays at 8 (shell, file_read, file_write, file_patch, file_diff, file_send, memory_graph_store, secret_get) — the new tools are not confirmation-capable.

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
- **THEN** exactly eight built-ins gate: `shell`, `file_read`, `file_write`, `file_patch`, `file_diff`, `file_send`, `memory_graph_store`, and `secret_get`
- **AND** `wait_for_any_agent` and `cancel_agent` are not in the confirmation-capable set

#### Scenario: shell cannot enter the shared approval set via the sub-agent approve-all path
- **GIVEN** the operator is offered approve-all buttons only for file tools (`file_read`, `file_write`, `file_patch`) in sub-agent confirmation prompts
- **WHEN** the operator grants an approve-all during a sub-agent confirmation
- **THEN** `shell` is never added to `auto_approve_tools` via this path
- **AND** `shell` remains always-blocked for sub-agents regardless of any approve-all grant
- **NOTE** Main-agent shell approve-all removal is deferred to the `shell-guard-v0` change; the per-prompt TTL already bounds any such grant to one prompt.

### Requirement: context_profile is a registered built-in tool

The built-in executor SHALL register `context_profile` as a built-in tool, routed by name to its handler. The tool returns a JSON snapshot of the current context-window consumption from the `ContextMonitor`. It is not confirmation-capable and is executed by the built-in executor's dispatch (not by the ReAct loop).

Feature: Builtin tool execution
Rule: The built-in set grows from 21 to 22 tools. The confirmation-capable set stays at 8.

#### Scenario: context_profile is enumerated as a built-in
- **GIVEN** the built-in executor
- **WHEN** a caller queries `is_builtin("context_profile")` or lists `all_tools`
- **THEN** `context_profile` is reported as a built-in

#### Scenario: context_profile is not confirmation-capable
- **GIVEN** the built-in executor
- **WHEN** the confirmation-capable built-ins are enumerated
- **THEN** exactly eight built-ins gate: `shell`, `file_read`, `file_write`, `file_patch`, `file_diff`, `file_send`, `memory_graph_store`, and `secret_get`
- **AND** `context_profile` is not in the confirmation-capable set

#### Scenario: context_profile returns context snapshot JSON
- **GIVEN** the agent calls `context_profile` with no arguments
- **WHEN** the tool executes
- **THEN** the result SHALL be a JSON object whose field contract is owned by the `context-monitoring` capability
- **AND** the result SHALL have `success = True`

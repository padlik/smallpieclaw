## ADDED Requirements

### Requirement: Shell-family availability is runtime-gated and disclosed to every prompt

The shared built-in executor MUST own the current shell capability for the main agent, scheduled prompts, and sub-agents. The shell family is `shell`, `shell_env_set`, `shell_env_unset`, `shell_env_list`, and `shell_env_get`. When capability is unavailable, the system MUST keep the agent and tools outside this family running, MUST omit every shell-family tool from model-callable definitions, semantic discovery, and prompt capability listings, and MUST include an explicit runtime prompt notice that shell is unavailable with actionable remediation. Scheduled runs MUST receive this same notice and guard; this requirement MUST NOT add job-level shell requirements or permissions or promise scheduled `last_error`/notification outcomes. A scheduled prompt that does not call shell may still be recorded successful by existing scheduler behavior; generic outcome correction is deferred to ARCH-03.

#### Scenario: Capability unavailable hides the complete shell family and informs prompt
- **GIVEN** the shared runtime shell capability is unavailable
- **WHEN** a main, scheduled, or sub-agent prompt is assembled
- **THEN** none of `shell`, `shell_env_set`, `shell_env_unset`, `shell_env_list`, or `shell_env_get` is offered or listed as a capability
- **AND** the prompt explicitly states shell is unavailable and gives actionable remediation
- **AND** non-shell tools remain available

#### Scenario: Capability available preserves current offers
- **GIVEN** the shared runtime shell capability is available
- **WHEN** a main, scheduled, or sub-agent prompt and its tool offers are assembled
- **THEN** shell-family visibility follows the existing caller-depth policy
- **AND** the prompt describes shell capability based on the current runtime snapshot, not a static platform/configuration claim

#### Scenario: Stale native or JSON shell call is rejected nonrecoverably
- **GIVEN** shell became unavailable after a model received a shell-family offer
- **WHEN** a native tool call or JSON-mode action invokes any shell-family tool
- **THEN** dispatch returns a nonrecoverable `shell_unavailable` error with actionable remediation
- **AND** no shell command or shell environment operation is performed
- **AND** no host subprocess or PTY fallback is attempted

#### Scenario: No shell confirmation is staged while unavailable
- **GIVEN** the shared runtime shell capability is unavailable
- **WHEN** a caller invokes `shell` or confirms a previously staged shell operation
- **THEN** the invocation or confirmation returns a nonrecoverable `shell_unavailable` error
- **AND** no confirmation token is newly staged and no command is executed

#### Scenario: Already staged confirmation is guarded after backend loss
- **GIVEN** a shell confirmation token was staged while nsjail was available
- **AND** the runtime capability becomes unavailable before confirmation
- **WHEN** the caller confirms the token
- **THEN** the operation returns a nonrecoverable `shell_unavailable` error
- **AND** no command executes through nsjail, subprocess, or PTY

#### Scenario: Shell built-in names remain reserved while hidden
- **GIVEN** the shell capability is unavailable
- **WHEN** an MCP server registers a tool named `shell` or any `shell_env_*` built-in name
- **THEN** the MCP tool is rejected as a built-in name collision
- **AND** the fixed built-in registry continues to recognize the shell-family names

## REMOVED Requirements

### Requirement: Dangerous and sensitive tools gate through two-phase confirmation

**Reason**: The requirement's nsjail-not-active fallback-confirmation scenario is obsolete: when nsjail is unavailable, shell must not run even after a confirmation. Its prior requirement name cannot be reused for the changed scenario set under the current OpenSpec validator.

**Migration**: Confirmation-capable non-shell tools keep their existing two-phase behavior. Shell confirmation modes apply only with healthy nsjail; if shell capability is unavailable, calls fail nonrecoverably with `shell_unavailable` and no host fallback.

## ADDED Requirements

### Requirement: Two-phase confirmation for dangerous and sensitive tools

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

When shell capability is unavailable, shell invocations MUST fail with nonrecoverable
`shell_unavailable` and MUST NOT stage confirmation or execute through a host backend, regardless
of the configured confirm mode.

The configurable gate applies only at depth 0 (main agent). Sub-agents (caller_depth >= 1) already
fail closed for shell commands and are unchanged by this configuration.

This amends ADR-0011's invariant that "shell is never auto-approved for the main agent" — the
`"adaptive"` and `"never"` modes allow the operator to relax shell confirmation when nsjail
sandboxing is active. The sub-agent fail-closed half of ADR-0011 is preserved.

Feature: Builtin tool execution
Rule: The confirmation gate for shell is configurable when nsjail is active. The operator chooses the trust level; absence of confinement is not a confirmation fallback.

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

#### Scenario: Shell unavailable does not fall back to confirmation and host execution
- **GIVEN** `shell_nsjail_confirm_mode` is `"never"` and nsjail is missing or unavailable
- **WHEN** the agent calls `shell("rm -rf /")` (category: `host_escape`)
- **THEN** the result is a nonrecoverable `shell_unavailable` error
- **AND** no confirmation token is staged
- **AND** the command does not execute through subprocess or PTY

#### Scenario: Sub-agents fail closed regardless of confirm mode
- **GIVEN** `shell_nsjail_confirm_mode` is `"never"` and nsjail is active
- **AND** a sub-agent (caller_depth >= 1) calls `shell("rm -rf build/")`
- **WHEN** the shell tool processes the call
- **THEN** the command is blocked (sub-agents fail closed for shell)
- **AND** the confirm mode setting does not affect sub-agent behavior

## MODIFIED Requirements

### Requirement: The built-in tool set includes shell env management tools

The built-in executor MUST register `shell_env_set`, `shell_env_unset`, `shell_env_list`, and
`shell_env_get` as built-in tools, routed by name to their handlers in `builtin_tools/shell_env.py`.
They are not confirmation-capable. Their fixed registration MUST NOT make them callable when shell
capability is unavailable; when unavailable, each invocation MUST fail nonrecoverably as
`shell_unavailable` and perform no shell-environment operation.

Feature: Builtin tool execution
Rule: The built-in set grows from 17 to 21 tools. The confirmation-capable set stays at 8; shell environment controls remain registered but availability-gated with shell.

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

#### Scenario: Shell environment tools reject calls when shell is unavailable
- **GIVEN** the shared runtime shell capability is unavailable
- **WHEN** a caller invokes any of `shell_env_set`, `shell_env_unset`, `shell_env_list`, or `shell_env_get`
- **THEN** the result is a nonrecoverable `shell_unavailable` error
- **AND** no shell environment state is read or changed

#### Scenario: Shell environment tools remain available with healthy nsjail
- **GIVEN** the shared runtime shell capability is available
- **WHEN** a caller invokes a registered shell environment tool within its existing caller-depth policy
- **THEN** the tool performs its existing shell-environment operation

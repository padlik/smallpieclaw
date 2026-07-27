## ADDED Requirements

### Requirement: shell_env_set persists a session-scoped environment variable

The `shell_env_set` built-in tool MUST set a key-value pair in the session-scoped `_shell_env` dict on the `BuiltinExecutor`. The variable MUST be injected via nsjail `-E` flags on every subsequent shell call in the same session. The `-E` flag overrides any config `envar` entry with the same key.

Feature: shell-env-management
Rule: Session env vars replace the non-persistent `export` pattern. Each nsjail invocation is a separate jail, so `export` does not persist. `shell_env_set` provides session-level persistence.

#### Scenario: shell_env_set makes variable visible in subsequent shell calls
- **GIVEN** the nsjail backend is active
- **WHEN** the agent calls `shell_env_set(key="PYTHONPATH", value="/home/user/projects/myproject/lib")`
- **AND** the agent calls `shell("echo $PYTHONPATH")`
- **THEN** the output is `/home/user/projects/myproject/lib`

#### Scenario: shell_env_set overrides base config envar
- **GIVEN** the base config has `envar: "PATH=/usr/bin:/bin"`
- **WHEN** the agent calls `shell_env_set(key="PATH", value="/custom/bin")`
- **AND** the agent calls `shell("echo $PATH")`
- **THEN** the output is `/custom/bin` (the `-E` flag overrides the config `envar`)

#### Scenario: shell_env_set does not affect the agent process environment
- **GIVEN** the agent process has `os.environ["HOME"] = "/home/user"`
- **WHEN** the agent calls `shell_env_set(key="HOME", value="/tmp")`
- **THEN** `os.environ["HOME"]` in the agent process is still `/home/user`
- **AND** only shell calls inside the jail see `HOME=/tmp`

### Requirement: shell_env_unset removes a session-scoped environment variable

The `shell_env_unset` built-in tool MUST remove a key from the session-scoped `_shell_env` dict. Subsequent shell calls MUST NOT inject the variable via `-E` flags. If the variable exists in the base config `envar`, the config value becomes visible again as the fallback.

#### Scenario: shell_env_unset removes variable from subsequent calls
- **GIVEN** the agent has called `shell_env_set(key="FOO", value="bar")`
- **WHEN** the agent calls `shell_env_unset(key="FOO")`
- **AND** the agent calls `shell("echo $FOO")`
- **THEN** the output is empty (the variable is no longer injected)

#### Scenario: shell_env_unset falls back to config envar
- **GIVEN** the base config has `envar: "PATH=/usr/bin:/bin"`
- **AND** the agent has called `shell_env_set(key="PATH", value="/custom/bin")`
- **WHEN** the agent calls `shell_env_unset(key="PATH")`
- **AND** the agent calls `shell("echo $PATH")`
- **THEN** the output is `/usr/bin:/bin` (the config `envar` fallback is visible again)

### Requirement: shell_env_list returns all session-scoped environment variables

The `shell_env_list` built-in tool MUST return the complete `_shell_env` dict as a JSON object. This includes only variables set via `shell_env_set`, not the base config `envar` entries or the agent process's `os.environ`.

#### Scenario: shell_env_list shows session variables
- **GIVEN** the agent has called `shell_env_set(key="PYTHONPATH", value="/lib")` and `shell_env_set(key="FOO", value="bar")`
- **WHEN** the agent calls `shell_env_list()`
- **THEN** the result contains `{"PYTHONPATH": "/lib", "FOO": "bar"}`
- **AND** the result does not contain base envars (PATH, HOME, LANG, TERM) unless explicitly set via `shell_env_set`

### Requirement: shell_env_get returns a single session-scoped environment variable

The `shell_env_get` built-in tool MUST return the value of a single key from the `_shell_env` dict. If the key is not in the dict, it MUST return an empty string or null.

#### Scenario: shell_env_get returns value for existing key
- **GIVEN** the agent has called `shell_env_set(key="FOO", value="bar")`
- **WHEN** the agent calls `shell_env_get(key="FOO")`
- **THEN** the result is `bar`

#### Scenario: shell_env_get returns empty for missing key
- **GIVEN** no `shell_env_set` has been called for key `MISSING`
- **WHEN** the agent calls `shell_env_get(key="MISSING")`
- **THEN** the result is empty or null

### Requirement: shell_env tools are not confirmation-capable

The `shell_env_set`, `shell_env_unset`, `shell_env_list`, and `shell_env_get` built-in tools MUST NOT gate through the confirmation flow. They modify an in-memory dict on the `BuiltinExecutor` — no filesystem, network, or subprocess operations are involved.

#### Scenario: shell_env tools execute without confirmation
- **GIVEN** the built-in executor with any `shell_nsjail_confirm_mode` setting
- **WHEN** the agent calls `shell_env_set`, `shell_env_unset`, `shell_env_list`, or `shell_env_get`
- **THEN** the operation executes immediately without a confirmation prompt
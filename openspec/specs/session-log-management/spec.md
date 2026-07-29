### Requirement: Shell output logs stored under XDG state home in per-conversation folders

Shell command output artifacts (large outputs exceeding `max_output`) MUST be saved to `~/.local/state/<agent>/session_logs/<conversation_id>/shell-<timestamp>-<hex>.log` instead of the old `data/shell_logs/` location. The `_open_shell_log` helper in `builtin_tools/shell.py` MUST compute the log directory from the `conversation_id` held by `BuiltinExecutor` (via `self._owner.conversation_id`) and the XDG state home path. The directory is created owner-only (0700) and files are owner-only (0600), same as the previous `shell_logs` behavior. The `_finalize_shell_log` helper keeps the file only if `total_chars > max_output`, otherwise deletes it — unchanged behavior.

Feature: session-log-management
Rule: The old `data/shell_logs/` directory is not migrated. Old logs are left in place (unattributable to any conversation). New logs start fresh in the new location.

#### Scenario: Large output saved to session_logs under conversation folder
- **GIVEN** the agent has `conversation_id` set to `abc123def456`
- **AND** the XDG state home is `~/.local/state`
- **AND** the agent name is `myagent`
- **WHEN** a shell command produces output exceeding `max_output`
- **THEN** the full output is saved to `~/.local/state/myagent/session_logs/abc123def456/shell-<ts>-<hex>.log`
- **AND** the file permissions are 0600 (owner read/write only)
- **AND** the directory permissions are 0700 (owner only)

#### Scenario: Small output does not create a persistent log file
- **GIVEN** the agent has `conversation_id` set to `abc123def456`
- **WHEN** a shell command produces output smaller than `max_output`
- **THEN** no log file persists in `session_logs/abc123def456/`
- **AND** any temporarily created file is deleted by `_finalize_shell_log`

#### Scenario: Tool output notice gives the real host path
- **GIVEN** a shell command produces output exceeding `max_output`
- **WHEN** the shell tool returns its result
- **THEN** the output includes `[full output saved to: <path>]`
- **AND** `<path>` is the real host path (e.g. `~/.local/state/myagent/session_logs/abc123def456/shell-xxx.log`)
- **AND** the same path is readable inside the nsjail jail (if active) via the read-only mount

### Requirement: session_logs folder mounted read-only inside the nsjail jail at the same host path

When the nsjail shell backend is active, the active conversation's `session_logs` folder MUST be bind-mounted read-only inside the jail at the same host path (`src == dst`, `is_bind: true`, `rw: false`, `mandatory: false`). The `NsjailConfigBuilder.build()` method receives `session_logs_dir` as a per-call kwarg. If `session_logs_dir` is empty or the directory does not exist, the mount is skipped (graceful degradation). This is a system mount — it bypasses `_BLOCKED_SYSTEM_PREFIXES` by construction (like `/dev/null` and `/dev/zero`), not via a blocklist exception.

Feature: session-log-management
Rule: The agent writes outside the jail; sandboxed shell commands read inside the jail. Read-only — a sandboxed script cannot write to or fill disk in session_logs.

#### Scenario: Shell can read a prior large output inside the jail
- **GIVEN** the nsjail backend is active and `conversation_id` is `abc123def456`
- **AND** a prior shell call saved a large output to `~/.local/state/myagent/session_logs/abc123def456/shell-xxx.log`
- **WHEN** the agent runs `shell("cat ~/.local/state/myagent/session_logs/abc123def456/shell-xxx.log")` inside the jail
- **THEN** the command succeeds and returns the saved output
- **AND** the nsjail config contains a read-only bind mount of the session_logs folder at its host path

#### Scenario: Shell cannot write to session_logs inside the jail
- **GIVEN** the nsjail backend is active and the session_logs folder is mounted read-only
- **WHEN** the agent runs `shell("echo data > ~/.local/state/myagent/session_logs/abc123def456/evil.log")` inside the jail
- **THEN** the write fails with a permission error
- **AND** no file is created on the host

#### Scenario: Empty session_logs_dir skips the mount
- **GIVEN** the nsjail backend is active but `session_logs_dir` is an empty string
- **WHEN** the nsjail config is generated
- **THEN** no session_logs mount entry appears in the config
- **AND** the jail starts normally

### Requirement: Age-based retention cleanup on startup

On startup, the agent MUST scan `~/.local/state/<agent>/session_logs/` and delete conversation folders whose newest file is older than `session_logs_retention_days` (default 7, configurable via `config.toml`). The corresponding `conversations/<old_id>.json` file MUST also be deleted when its session_logs folder is removed. Folders that do not match any existing or recent conversation are cleaned up; the active conversation's folder is always preserved. If the `session_logs/` directory does not exist (first startup), the cleanup is a no-op — no error is logged.

Feature: session-log-management

#### Scenario: Old session_logs folder is deleted on startup
- **GIVEN** `session_logs_retention_days` is 7
- **AND** a session_logs folder `old_conv_id/` has files older than 7 days
- **WHEN** the agent starts up
- **THEN** the `old_conv_id/` folder and its contents are deleted
- **AND** the corresponding `conversations/old_conv_id.json` is deleted

#### Scenario: Active conversation folder is preserved
- **GIVEN** `session_logs_retention_days` is 7
- **AND** the active `conversation_id` folder has files older than 7 days
- **WHEN** the agent starts up
- **THEN** the active conversation's folder is NOT deleted

#### Scenario: Retention is configurable
- **GIVEN** `session_logs_retention_days` is set to 30 in config.toml
- **WHEN** the agent starts up
- **THEN** folders with files older than 30 days are deleted
- **AND** folders with files newer than 30 days are preserved

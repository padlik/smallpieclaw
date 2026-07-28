## Why

The nsjail sandbox mounts the agent's own code directory (`_AGENT_DIR`, e.g. `/home/paul/piclaw/`) as a read-write `project_dir` and sets it as `cwd`. This exposes all agent internals — source code, memory store, config, scheduler state, tool index — to any sandboxed shell command. A compromised or buggy LLM-issued command can read secrets, corrupt memory, or overwrite agent code. Additionally, the blanket `/home` entry in `_BLOCKED_SYSTEM_PREFIXES` prevents legitimate user paths (skills, workspace, trusted dirs) from being mounted in a systemd-user deployment where everything naturally lives under `/home`. Finally, the vault file lives under `XDG_DATA_HOME` while all other agent state lives under `XDG_STATE_HOME`, creating unnecessary operational fragmentation.

### Alternatives Considered

1. **Keep `project_dir` mount, add seccomp/permission filtering** — rejected: too complex, fragile, and doesn't fix the fundamental issue that agent code shouldn't be in the sandbox.
2. **Replace `project_dir` with `workspace_dir` as the RW mount** — rejected: `workspace_dir` (`~/Documents`) is too broad and shouldn't be auto-mounted; user files should go through the trusted-dirs approval mechanism.
3. **Make `project_dir` configurable, default to `workspace_dir`** — rejected: no directory should be blanket-mounted RW by default. The sandbox should only get `/tmp`, system dirs, and explicitly-approved trusted dirs.

## What Changes

- **BREAKING**: Remove the `project_dir` RW mount from the nsjail sandbox. The agent's code directory is no longer mounted inside the jail. Sandbox `cwd` changes from `project_dir` to `/tmp` (the session tmpdir, already mounted RW).
- **BREAKING**: Remove the `nsjail_project_dir` parameter from `BuiltinExecutor.__init__` and the `nsjail_project_dir=_AGENT_DIR` wiring in `main.py`. Remove the `project_dir` parameter from `NsjailConfigBuilder.__init__`.
- Remove `/home` from `_BLOCKED_SYSTEM_PREFIXES` in `nsjail_config.py`. The targeted blocks in `_blocked_user_prefixes` (`~/.ssh`, `~/.local`, `~/.config`) already cover sensitive subdirs. Add `~/.gnupg` to `_blocked_user_prefixes` for extra safety.
- Add a project-dir carve-out in `_load_trusted_mounts`: trusted-dir entries that are under a known project/workspace directory are silently skipped (already accessible) rather than rejected with a warning. This prevents redundant mount attempts and noise.
- **BREAKING**: Move the vault file default from `~/.local/share/<agent_name>/secrets.toml` (XDG_DATA_HOME) to `~/.local/state/<agent_name>/secrets.toml` (XDG_STATE_HOME), consolidating all agent state under one XDG directory. Add one-time migration: if the old path exists and the new path doesn't, copy it. If both paths exist, prefer the new path and log a warning that the old path is stale and can be removed manually.
- Fix `shell_env` tool return contract: all four `shell_env` tools now return `output` and `error` keys (already implemented).
- Mount `/dev/null` and `/dev/zero` inside the sandbox for shell redirections (already implemented).
- Harden `react_loop.py` to use `.get()` instead of direct subscript for outcome dict keys (already implemented).

## Capabilities

### New Capabilities

_(none — all changes modify existing capabilities)_

### Modified Capabilities

- `nsjail-shell-sandboxing`: The "Project directory is mounted at its original host path" requirement is **removed**. The sandbox no longer mounts any project directory by default. The `cwd` inside the jail is `/tmp`. The "Trusted directories are mounted at their original host paths" requirement is modified: trusted dirs under `/home` are now accepted (the blanket `/home` block is removed; targeted sensitive-subdir blocks remain). A new requirement covers `/dev/null` and `/dev/zero` minimal device mounts.
- `skills-dir-sandbox-mount`: The "nested under project directory" containment logic is removed (there is no project directory). The skills_dir RO mount is now governed solely by the blocklist (which no longer blocks `/home`). The mount is emitted whenever `skills_dir` exists and is not a blocked sensitive path.
- `agent-scoped-directories`: The vault path default changes from `~/.local/share/<agent_name>/secrets.toml` to `~/.local/state/<agent_name>/secrets.toml`. The rule "vault lives in `~/.local/share/<agent_name>/`" is updated to `~/.local/state/<agent_name>/`. A migration scenario is added for existing deployments.
- `shell-env-management`: The `shell_env_set`, `shell_env_unset`, `shell_env_list`, and `shell_env_get` tools now return `output` and `error` keys in their result dicts, conforming to the standard tool outcome contract.
- `secure-secret-resolution`: Scenarios referencing the vault path `~/.local/share/<agent_name>/secrets.toml` are updated to `~/.local/state/<agent_name>/secrets.toml` to reflect the consolidated XDG_STATE_HOME location.
- `file-access-zones`: Scenarios referencing the vault path `~/.local/share/<agent>/secrets.toml` as an UNRECOGNISED path are updated to the new `~/.local/state/<agent>/secrets.toml` location. This includes both the vault file path scenario and the `~/.local/share/<agent>/` trusted-dir example scenario.

## Impact

- **`nsjail_config.py`**: Remove `project_dir` parameter and RW mount; change `cwd` to `/tmp`; remove `/home` from `_BLOCKED_SYSTEM_PREFIXES`; add `~/.gnupg` to `_blocked_user_prefixes`; add project-dir carve-out in `_load_trusted_mounts`; `/dev/null` and `/dev/zero` mounts (already done).
- **`builtin_executor.py`**: Remove `nsjail_project_dir` parameter and its forwarding to `NsjailConfigBuilder`.
- **`main.py`**: Remove `nsjail_project_dir=_AGENT_DIR` wiring; add vault migration from old XDG_DATA_HOME path to new XDG_STATE_HOME path.
- **`config_schema.py`**: Change `vault_path()` default from `~/.local/share/<agent>/secrets.toml` to `~/.local/state/<agent>/secrets.toml`.
- **`builtin_tools/shell_env.py`**: Return contract fix (already done).
- **`react_loop.py`**: `.get()` hardening (already done).
- **Tests**: `test_nsjail_config.py` (20+ tests asserting `project_dir` mount and `cwd`), `test_builtin_executor.py` (shell_env tests, already updated), `tests/nsjail/test_nsjail_mounts.py` (project dir mount tests, shell_logs test), `test_config_schema.py` (vault path tests), `test_access_control.py` (trusted dirs tests).
- **Existing deployments**: Operators with vault at `~/.local/share/<agent>/secrets.toml` get auto-migrated. Operators relying on shell commands that read/write files under the agent code directory without an explicit trusted-dir entry will need to add those paths via `/dir add`. This is the intended security improvement.
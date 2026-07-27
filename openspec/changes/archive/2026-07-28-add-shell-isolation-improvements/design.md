## Context

The agent currently uses a string `shell_nsjail_network` field (`"none"` | `"host"`) to control nsjail network isolation. This is non-intuitive and easy to misconfigure. A boolean `allow_net` with clear `True`/`False` semantics improves UX and reduces config errors.

Additionally, the `skills/` directory is referenced in the system prompt as a source of scripts and binaries, but it is not mounted inside the nsjail sandbox. Commands like `cd <skills_dir> && ./scripts/run.sh` fail with "No such file or directory". Mounting `skills_dir` read-only inside the jail fixes this without expanding the writable blast radius.

The `shell_logs` directory (`data/shell_logs/`) is an internal overflow directory used for large command outputs. It is not intended for inter-script exchange and should not be mounted inside the sandbox.

This change is coherent with ADR-0012 (nsjail shell isolation with configurable confirmation), which is currently in force. ADR-0012 established the `shell_nsjail_confirm_mode` field and the confirmation-gate rules. This design amends the network-isolation control mechanism and adds a read-only skills mount, both of which are additive to ADR-0012's framework.

## Goals / Non-Goals

**Goals:**
- Replace the string `shell_nsjail_network` with a boolean `allow_net` (default `false`) for clearer UX.
- Mount `skills_dir` read-only inside the nsjail sandbox so skill scripts referenced in AVAILABLE SKILLS are accessible to shell commands.
- Do NOT mount `shell_logs` inside the sandbox (it is internal overflow, not an exchange surface).
- Update confirmation logic in `builtin_tools/shell.py` to check `allow_net` instead of `shell_nsjail_network == "none"`.
- Update all tests to match the new field and mount behavior.

**Non-Goals:**
- No changes to nsjail backend selection (`shell_backend` remains `"subprocess"` | `"pty"` | `"nsjail"`).
- No changes to cgroup/rlimit resource limit logic.
- No changes to trusted_dirs mount logic.
- No new shell backends or network connectivity modes (pasta, loopback-only, etc.).

## Decisions

### Decision 1: Boolean `allow_net` replaces string `shell_nsjail_network`

**Rationale:** A boolean is unambiguous. `allow_net = false` (default) means network is isolated; `allow_net = true` means the jail shares the host network namespace. The old string values `"none"` / `"host"` required operators to know nsjail internals.

**Alternatives considered:**
- Keep string but add validation → rejected: still non-intuitive.
- Enum-like dataclass field → rejected: overkill for a two-state toggle; boolean is simpler and matches the existing `shell_streaming`, `diagnose_empty_responses` boolean fields in `AgentConfig`.

**Impact:** `AgentConfig` gains `allow_net: bool = False` and loses `shell_nsjail_network: str`. `_parse_agent()` uses `_parse_bool()` for validation.

### Decision 2: `skills_dir` mounted read-only, not read-write

**Rationale:** Skills are scripts and binaries referenced in the system prompt. They are assets, not working directories. Read-only mounting prevents a sandboxed shell from accidentally or maliciously modifying skill files, while still making them executable.

**Alternatives considered:**
- Mount read-write → rejected: expands blast radius unnecessarily.
- Do not mount → rejected: breaks skill script execution inside the jail.

**Impact:** `NsjailConfigBuilder` receives `skills_dir: str` in its constructor. In `build()`, if the directory exists, a mount line is emitted with `rw: false`. If `skills_dir` is nested under `project_dir` (the common case when `skills_dir` is relative to agent home), the mount is skipped and a warning is logged because the project directory is already mounted read-write at the same path — a second read-only mount for a subdirectory would create a conflict.

### Decision 3: `shell_logs` is excluded from sandbox mounts

**Rationale:** `shell_logs` is an internal overflow directory for large command outputs. It is not used for script-to-script file exchange, and exposing it inside the jail is unnecessary. The agent process (outside the jail) handles artifact log lifecycle.

**Impact:** No code change needed — `shell_logs` is not currently mounted, and we explicitly preserve this omission.

### Decision 4: `_should_confirm` checks `allow_net` instead of `shell_nsjail_network`

**Rationale:** The adaptive confirmation mode skips `network`-category confirmations when the sandbox has network isolation. With the boolean field, the check becomes `not self._owner._allow_net` (network is isolated).

**Impact:** `builtin_tools/shell.py` line ~74 changes from `self._owner._shell_nsjail_network == "none"` to `not self._owner._allow_net`. The `builtin_executor.py` constructor and `main.py` wiring pass `allow_net` instead of `shell_nsjail_network`.

### Decision 5: `NsjailConfigBuilder` receives `skills_dir` and `allow_net` as constructor parameters

**Rationale:** The builder needs both the skills directory path and the network isolation flag at config-generation time. Passing them at construction keeps the `build()` method focused on per-call dynamic parameters (command, timeout, env).

**Impact:** `NsjailConfigBuilder.__init__` adds `skills_dir: str = ""` and changes `network: str` to `allow_net: bool = False`. `build()` checks `os.path.isdir(self.skills_dir)` and `os.path.commonpath()` before emitting the mount, and uses `self.allow_net` to generate `clone_newnet`.

## Risks / Trade-offs

- **[Risk]** Breaking change for existing configs using `shell_nsjail_network`.
  -> **Mitigation:** `parse_config()` raises `ConfigError` with a clear migration message if `shell_nsjail_network` is present in the raw config, directing the user to replace it with `allow_net = true/false`.
- **[Risk]** `skills_dir` may not exist at runtime (e.g., user deleted it after startup).
  -> **Mitigation:** `build()` checks `os.path.isdir()` before emitting the mount; if the directory is missing, the mount is silently skipped and a debug log is emitted.
- **[Risk]** `skills_dir` is nested under `project_dir` (common default), causing a mount conflict.
  -> **Mitigation:** `build()` checks `os.path.commonpath([skills_dir, project_dir]) == project_dir`; if true, the mount is skipped and a warning is logged. The directory is already accessible via the project mount.
- **[Risk]** Read-only mount of `skills_dir` may break workflows that expect to write temporary files next to skill scripts.
  -> **Mitigation:** Documented as non-goal; operators who need RW access can add the skills directory via `/dir add` as a trusted RW mount.

## Migration Plan

1. **Config migration:** Operators with `shell_nsjail_network = "host"` must change to `allow_net = true`. Operators with `shell_nsjail_network = "none"` (or omitted) must change to `allow_net = false` (or omit it, since `false` is the default).
2. **Code deployment:** The change is a config-schema + mount-wiring update. No database migration, no external dependency changes.
3. **Rollback:** Revert the commit; the old `shell_nsjail_network` string logic is removed, so rollback requires reverting config files too.

## Open Questions

- None at this time. The design is additive to ADR-0012 and does not require superseding any in-force ADRs.

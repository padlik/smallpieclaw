## Why

The shell built-in tool currently runs commands with no kernel-level isolation — the agent process's full filesystem, network, and environment are exposed to every command. The only protection is a regex denylist (`_is_dangerous_shell`) and operator confirmation. A confused or compromised agent can destroy the host filesystem, exfiltrate secrets via network, or exhaust resources. Google's nsjail provides Linux namespace-based sandboxing (mount, PID, net, user, IPC, UTS, cgroup), seccomp-bpf syscall filtering, and cgroup v2 resource limits — all verified working on both a leased Ubuntu playground and a local Lima VM on Apple Silicon. This change adds nsjail as a third shell backend, making the shell tool kernel-sandboxed by default on Linux hosts while preserving the existing subprocess/pty backends as fallbacks.

## What Changes

- **New shell backend**: `shell_backend: "nsjail"` alongside existing `"subprocess"` and `"pty"`. When active, shell commands run inside an nsjail sandbox with mount/PID/net/user/IPC/UTS/cgroup namespace isolation, seccomp-bpf syscall filtering, and cgroup v2 resource limits (memory, PIDs, CPU).
- **Mount at original host path**: The project directory is bind-mounted at its original host path inside the jail (e.g., `/home/user/projects/myproject` → same path). This eliminates path translation — file tools and shell tools see identical paths. nsjail auto-creates parent directory scaffolds.
- **Trusted directory mounts**: User-configurable additional directories (from the existing `trusted_dirs.json` list managed by `/dir` commands) are bind-mounted at their original host paths inside the jail, respecting their existing `mode: "rw"` or `mode: "r"` setting. No new config field — the single source of truth is `data/trusted_dirs.json`.
- **Per-session /tmp**: A per-session temp directory (created at agent startup) is bind-mounted as `/tmp` inside every jail, persisting files across nsjail invocations within a session.
- **Dynamic config generation**: nsjail config is generated per shell call as a tempfile (static parts + dynamic parts). No base config file. ~0.2ms per call.
- **Cgroup delegation with fallback**: Tier 1 uses `systemd-run --user --scope --property=Delegate=yes` + explicit `cgroupv2_mount` for hard memory/CPU/PID limits. Tier 2 falls back to rlimits only when systemd or cgroup v2 is unavailable.
- **System mount auto-detection**: At startup, detect host filesystem layout (symlinks vs real dirs for `/bin`, `/lib`, etc.) and generate appropriate mount entries with `mandatory: false` for symlinks.
- **Configurable confirmation flow**: New `shell_nsjail_confirm_mode` config field (`"always"` | `"adaptive"` | `"never"`). Patterns get a category tag (`host_escape`, `network`, `resource`, `project`, `policy`). `"always"` (default) confirms all dangerous patterns (backward-compatible). `"adaptive"` skips `resource` patterns (kernel-bounded). `"never"` auto-approves when nsjail is active. Falls back to `"always"` when nsjail is not active. The configurable gate applies only at depth 0 (main agent); sub-agents (depth ≥ 1) already fail closed for shell commands and are unchanged.
- **Environment variable isolation**: `keep_env: false` (nsjail default) — shell does NOT inherit the agent process's `os.environ`. A base set of envars (`PATH`, `HOME`, `LANG`, `TERM`) is always injected via nsjail config `envar` entries as a fallback. Only explicitly injected vars are visible. Security improvement: API keys, tokens are not leaked into shell commands.
- **Session env management**: New built-in tools `shell_env_set`, `shell_env_unset`, `shell_env_list`, `shell_env_get` manage a session-scoped env dict injected via nsjail `-E` flags per call. `-E` flags override config `envar` entries. Replaces the non-persistent `export` pattern.
- **Test infrastructure**: Lima VM-based integration test suite for macOS development. Session-scoped pytest fixture creates/starts a Lima VM, provisions nsjail, runs tests via `limactl shell`. VM persists between runs.

## Capabilities

### New Capabilities

- `nsjail-shell-sandboxing`: nsjail-based shell command isolation — namespace isolation, cgroup resource limits, seccomp-bpf filtering, mount-at-original-path, trusted dir mounts, per-session /tmp, dynamic config generation, cgroup delegation fallback, system mount auto-detection.
- `shell-env-management`: Session-scoped environment variable management for the shell tool — `shell_env_set`, `shell_env_unset`, `shell_env_list`, `shell_env_get` built-in tools, injected via nsjail `-E` flags, replacing non-persistent `export`.

### Modified Capabilities

- `builtin-tool-execution`: The shell tool gains a third backend (`nsjail`) and the confirmation gate becomes configurable via `shell_nsjail_confirm_mode`. The dangerous-pattern detection (`_is_dangerous_shell`) returns a 3-tuple `(dangerous, reason, category)` instead of a 2-tuple. Four new built-in tools (`shell_env_set/unset/list/get`) are added to the built-in tool set.
- `trusted-dir-management`: Trusted directories are now also used as nsjail mount points for the shell sandbox, in addition to their existing role in file-access-zone classification.

## Impact

- **`builtin_tools/shell.py`**: New `_run_shell_nsjail()` method (reuses subprocess select loop). Modified `_exec_shell()` for configurable confirmation. New `_should_confirm()` helper.
- **`builtin_tools/patterns.py`**: `_is_dangerous_shell` returns 3-tuple with category. 15 patterns get category tags.
- **`builtin_executor.py`**: New constructor params (`shell_nsjail_confirm_mode`, `_shell_env` dict, `_shell_nsjail_active` flag). New `NsjailConfigBuilder` instance.
- **`config_schema.py`**: New `AgentConfig` fields: `shell_nsjail_confirm_mode`, `shell_nsjail_memory_mb`, `shell_nsjail_pids_max`, `shell_nsjail_cpu_percent`, `shell_nsjail_network`.
- **`main.py`**: Pass new config fields to `BuiltinExecutor`. Create per-session temp dir at startup and clean it up at agent shutdown. Detect cgroup delegation capability at startup. Load trusted dirs from `data/trusted_dirs.json` for nsjail mount generation.
- **New module `nsjail_config.py`**: `NsjailConfigBuilder` class — system mount detection, config generation, cgroup delegation detection, nsjail command building.
- **New module `builtin_tools/shell_env.py`**: `shell_env_set/unset/list/get` handlers.
- **`builtin_tools/descriptors.py`**: Register 4 new built-in tools.
- **`tests/`**: New `test_nsjail_integration.py` with Lima VM fixture. Updated `test_builtin_executor.py` for 3-tuple return.
- **Platform**: nsjail backend is Linux-only. On macOS, falls back to subprocess. The Lima VM is for development/testing only — production Linux hosts run nsjail as a local subprocess.
- **Dependencies**: nsjail binary must be installed on the host (built from source or packaged). Not a Python dependency.
## 1. Config Schema & Plumbing

- [x] 1.1 Add `shell_nsjail_confirm_mode: str = "always"` to `AgentConfig` in `config_schema.py` (values: `"always"`, `"adaptive"`, `"never"`)
- [x] 1.2 Add `shell_nsjail_memory_mb: int = 256`, `shell_nsjail_pids_max: int = 64`, `shell_nsjail_cpu_percent: int = 50`, `shell_nsjail_network: str = "none"` to `AgentConfig`
- [x] 1.3 Add parsing for all new fields in `_parse_agent_config()` in `config_schema.py`
- [x] 1.4 Pass new fields through `main.py` to `BuiltinExecutor.__init__()`
- [x] 1.5 Add `_shell_env: dict[str, str]`, `_shell_nsjail_confirm_mode: str`, `_shell_nsjail_active: bool` fields to `BuiltinExecutor.__init__()`
- [x] 1.6 Create per-session temp dir in `main.py` at startup, pass to `BuiltinExecutor`, clean up at shutdown

## 2. NsjailConfigBuilder Module

- [x] 2.1 Create `nsjail_config.py` with `NsjailConfigBuilder` class
- [x] 2.2 Implement `_detect_system_mounts()` — auto-detect symlinks vs real dirs for `/bin`, `/lib`, `/sbin`, `/lib64`, `/lib32`; mount `/usr` first, others with `mandatory: false` if symlinks
- [x] 2.3 Implement `_detect_cgroup_capability()` — check `shutil.which("systemd-run")`, `statfs(/sys/fs/cgroup)` for `CGROUP2_SUPER_MAGIC`, detect user cgroup path `/sys/fs/cgroup/user.slice/user-<uid>.slice/user@<uid>.service`
- [x] 2.4 Implement `build(command, timeout)` — generate full nsjail config as tempfile: static parts (namespaces, seccomp, system mounts, base envars, `keep_env: false`) + dynamic parts (time_limit, cwd, project mount, trusted mounts, /tmp mount, command). Return `(cfg_path, nsjail_cmd)`.
- [x] 2.5 Add `-E` flag generation from `_shell_env` dict to the nsjail command
- [x] 2.6 Add `systemd-run --user --scope --property=Delegate=yes` wrapper when cgroup delegation is available; raw nsjail command when falling back to rlimits
- [x] 2.7 Load trusted dirs from `data/trusted_dirs.json` at call time (read fresh, not cached) and generate mount entries with `rw: true` for `mode: "rw"`, `rw: false` for `mode: "r"`
- [x] 2.8 Add base envars to config: `envar: "PATH=..."`, `envar: "HOME=..."`, `envar: "LANG=..."`, `envar: "TERM=..."`
- [x] 2.9 Generate resource-limit config entries: Tier 1 (cgroup delegation available) — `cgroup_mem_max` from `shell_nsjail_memory_mb`, `cgroup_pids_max` from `shell_nsjail_pids_max`, `cgroup_cpu_ms_per_sec` from `shell_nsjail_cpu_percent`; Tier 2 (fallback) — `rlimit_as`, `rlimit_cpu`, `rlimit_fsize`, `rlimit_nofile`
- [x] 2.10 Wire `shell_nsjail_network` config field to `clone_newnet` in the generated config: `"none"` → `clone_newnet: true`, `"host"` → `clone_newnet: false`

## 3. Shell Backend Integration

- [x] 3.1 Add `_run_shell_nsjail()` method to `ShellTools` in `builtin_tools/shell.py` — reuses the exact same select() loop, output truncation, artifact logging, and error classification as `_run_shell_subprocess`. Only the `subprocess.Popen` command changes.
- [x] 3.2 Add nsjail backend dispatch to `_run_shell()`: `if self._owner._shell_backend == "nsjail" and self._owner._shell_nsjail_active: return self._run_shell_nsjail(...)`
- [x] 3.3 Add runtime detection in `_run_shell_nsjail()`: if nsjail binary not found, log warning and fall back to `_run_shell_subprocess()`
- [x] 3.4 Set `_shell_nsjail_active` at startup: `shell_backend == "nsjail" and shutil.which(nsjail_path) is not None`
- [x] 3.5 Clean up config tempfile in `_run_shell_nsjail()` after command completes (success, failure, or timeout) — use try/finally

## 4. Pattern Categories & Configurable Confirmation

- [x] 4.1 Add category as third element to all 15 patterns in `_DANGEROUS_SHELL_PATTERNS` in `builtin_tools/patterns.py` (categories: `host_escape`, `network`, `resource`, `project`, `policy`)
- [x] 4.2 Change `_is_dangerous_shell()` return type from `tuple[bool, str]` to `tuple[bool, str, str]` — return `(dangerous, reason, category)`
- [x] 4.3 Add `_should_confirm(category)` method to `ShellTools` — returns `True` if mode is `"always"` or nsjail not active; `False` if mode is `"never"` and nsjail active; for `"adaptive"`, returns `category not in {"resource"}` when nsjail active
- [x] 4.4 Modify `_exec_shell()` to use 3-tuple from `_is_dangerous_shell()` and gate via `_should_confirm(category)`
- [x] 4.5 Update re-export in `builtin_executor.py` (line 54: `_is_dangerous_shell` re-export for tests)

## 5. Shell Env Management Built-in Tools

- [x] 5.1 Create `builtin_tools/shell_env.py` with `ShellEnvTools` class holding back-reference to `BuiltinExecutor`
- [x] 5.2 Implement `shell_env_set(key, value)` — sets `self._owner._shell_env[key] = value`, returns `{"success": True}`
- [x] 5.3 Implement `shell_env_unset(key)` — removes key from `_shell_env` if present, returns `{"success": True}`
- [x] 5.4 Implement `shell_env_list()` — returns `{"success": True, "env": dict(self._owner._shell_env)}`
- [x] 5.5 Implement `shell_env_get(key)` — returns `{"success": True, "value": self._owner._shell_env.get(key, "")}`
- [x] 5.6 Register all 4 tools in `builtin_tools/descriptors.py` with appropriate JSON schemas
- [x] 5.7 Wire dispatch in `builtin_executor.py` — add handlers to the dispatch table

## 6. Test Updates

- [x] 6.1 Update `tests/test_builtin_executor.py` — change all 30 `_is_dangerous_shell()` call sites from 2-tuple to 3-tuple unpacking (`flagged, reason, _ = ...` or `flagged, _, _ = ...`)
- [x] 6.2 Add tests for `_should_confirm()` — test all three modes (`always`, `adaptive`, `never`) with nsjail active and inactive
- [x] 6.3 Add tests for `shell_env_set/unset/list/get` — verify dict operations, no confirmation gating, no side effects on `os.environ`
- [x] 6.4 Add tests for `NsjailConfigBuilder._detect_system_mounts()` — mock symlink detection, verify `mandatory: false` for symlinks
- [x] 6.5 Add tests for `NsjailConfigBuilder.build()` — verify config contains correct time_limit, cwd, mounts, envars, seccomp, command

## 7. Lima VM Integration Tests

- [x] 7.1 Create `tests/nsjail/conftest.py` with session-scoped `nsjail_vm` fixture — check/create/start Lima VM, provision nsjail (one-time), apply AppArmor fix
- [x] 7.2 Create `tests/nsjail/test_nsjail_behavior.py` — test streaming, exit code fidelity, timeout enforcement, stdout/stderr separation, large output
- [x] 7.3 Create `tests/nsjail/test_nsjail_mounts.py` — test mount at original path, RW workspace bind mount, RO trusted dir, unmounted paths invisible, deep nested paths, per-session /tmp persistence across invocations
- [x] 7.4 Create `tests/nsjail/test_nsjail_cgroups.py` — test cgroup delegation via systemd-run, memory limit enforcement, PID limit enforcement
- [x] 7.5 Create `tests/nsjail/test_nsjail_env.py` — test `keep_env: false` (secrets not visible), base envars present, `-E` flag injection, session env persistence across calls
- [x] 7.6 Add `tests/nsjail/` to test discovery but skip when Lima is not installed (`pytest.skip`)

## 8. Lint & Validate

- [x] 8.1 Run `ruff check .` and fix any issues
- [x] 8.2 Run `vulture . vulture_whitelist.py --min-confidence 80` and update whitelist if needed
- [x] 8.3 Run `make test` and ensure all existing tests pass (no regressions from 3-tuple change)
- [x] 8.4 Run `openspec validate add-nsjail-shell-isolation --type change --strict` and fix any validation errors
## 1. Remove project_dir mount from nsjail sandbox

- [x] 1.1 Remove `project_dir` parameter from `NsjailConfigBuilder.__init__` and the RW mount + `cwd` lines in `build()`. Set `cwd` to `/tmp` instead.
- [x] 1.2 Remove `nsjail_project_dir` parameter from `BuiltinExecutor.__init__` and its forwarding to `NsjailConfigBuilder`.
- [x] 1.3 Remove `nsjail_project_dir=_AGENT_DIR` wiring in `main.py` (line 297).
- [x] 1.4 Remove the `project_dir` containment logic in `NsjailConfigBuilder.build()` (the `os.path.commonpath` block for skills_dir vs project_dir — search for `os.path.commonpath` near skills_dir logic, line numbers will shift after task 1.1). Skills_dir mount is now governed solely by the blocklist.
- [x] 1.5 Update the system prompt (in `prompt_builder.py` or `prompts/`) to inform the LLM that `cwd` inside the sandbox is `/tmp` and that host file access via shell requires trusted-dir approval.

## 2. Fix /home blocklist

- [x] 2.1 Remove `/home` from `_BLOCKED_SYSTEM_PREFIXES` in `nsjail_config.py` (line 25).
- [x] 2.2 Add `~/.gnupg` to `_blocked_user_prefixes` in `nsjail_config.py` (line 64-68).
- [x] 2.3 Add a carve-out in `_load_trusted_mounts`: if a trusted-dir entry is under `session_tmpdir` or an already-accepted trusted mount, silently skip it (log debug) rather than emitting a redundant mount entry.

## 3. Consolidate vault to XDG_STATE_HOME

- [x] 3.1 Change `vault_path()` in `config_schema.py` (line 231) from `~/.local/share/<agent>/secrets.toml` to `~/.local/state/<agent>/secrets.toml`.
- [x] 3.2 Add vault migration in `main.py` after `trusted_dirs_path` migration block: if old path (`~/.local/share/<agent>/secrets.toml`) exists and new path doesn't, copy it and log info. If both exist, log warning that old path is stale.
- [x] 3.3 Update the vault path comment/docstring in `config_schema.py` from `~/.local/share/<agent_name>/` to `~/.local/state/<agent_name>/`.

## 4. Update tests for project_dir removal

- [x] 4.1 Update `tests/test_nsjail_config.py`: remove or rewrite all tests that assert `project_dir` mount, `project_dir` as `cwd`, or `project_dir` containment logic for skills_dir. Tests should assert `cwd: "/tmp"` and no project_dir mount.
- [x] 4.2 Update `tests/nsjail/test_nsjail_mounts.py`: remove or rewrite `test_project_dir_mount_original_path` and any project_dir-related mount tests. Update `test_shell_logs_not_mounted` if it relies on project_dir mount.
- [x] 4.3 Update `tests/test_nsjail_config.py`: add tests for trusted dirs under `/home` being accepted (not rejected by blocklist).
- [x] 4.4 Update `tests/test_nsjail_config.py`: add test for `~/.gnupg` being rejected by `_blocked_user_prefixes`.
- [x] 4.5 Update `tests/test_nsjail_config.py`: add/update skills_dir mount tests for the new blocklist-only logic — (a) skills_dir under `/home` is accepted and mounted RO, (b) skills_dir on a blocked user prefix (`~/.local/...`) is rejected.

## 5. Update tests for vault path change

- [x] 5.1 Update `tests/test_config_schema.py`: change vault path assertions from `~/.local/share/<agent>/secrets.toml` to `~/.local/state/<agent>/secrets.toml`.
- [x] 5.2 Add test for vault migration: old path exists, new path doesn't → copy occurs.
- [x] 5.3 Add test for vault migration: both paths exist → warning logged, new path preferred.
- [x] 5.4 Update `tests/test_access_control.py`: update vault path references from `~/.local/share/<agent>/` to `~/.local/state/<agent>/`.

## 6. Verify already-implemented fixes

- [x] 6.1 Verify `shell_env` return contract fix: `builtin_tools/shell_env.py` returns `output` and `error` keys in all 4 tools. Run `pytest tests/test_builtin_executor.py::TestShellEnv -v`.
- [x] 6.2 Verify `/dev/null` and `/dev/zero` mounts in `nsjail_config.py`. Run `pytest tests/nsjail/ -v`.
- [x] 6.3 Verify `react_loop.py` `.get()` hardening. Run `pytest tests/test_react_loop.py -v`.

## 7. Lint and full test suite

- [x] 7.1 Run `ruff check .` — must pass clean.
- [x] 7.2 Run `vulture . vulture_whitelist.py --min-confidence 80 --exclude interfaces.py` — must pass clean. Update `vulture_whitelist.py` if new public API symbols are flagged.
- [x] 7.3 Run `make test` — full test suite must pass.
- [x] 7.4 Run `openspec validate nsjail-directory-access --type change --strict` — must pass before archive.
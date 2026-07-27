## 1. Config Schema — Replace shell_nsjail_network with allow_net

- [x] 1.1 Add `allow_net: bool = False` field to `AgentConfig` dataclass in `config_schema.py`
- [x] 1.2 Remove `shell_nsjail_network: str` field from `AgentConfig` dataclass
- [x] 1.3 Update `_parse_agent()` to parse `allow_net` with `_parse_bool()` (default `False`)
- [x] 1.4 In `_reject_removed_fields()`, add a check for `shell_nsjail_network` in `raw.get("agent", {})` with a migration message directing the operator to replace it with `allow_net = true/false`
- [x] 1.5 Update `AgentConfig` docstring that still references `shell_nsjail_network` to reference `allow_net`
- [x] 1.6 Update `tests/test_config_schema.py` assertions for the new field and removed field

## 2. Nsjail Config Builder — Add skills_dir mount

- [x] 2.0 Change `NsjailConfigBuilder.__init__` parameter `network: str` to `allow_net: bool = False`
- [x] 2.0b Update `build()` to use `self.allow_net` instead of `self.network` for `clone_newnet`
- [x] 2.1 Add `skills_dir: str = ""` parameter to `NsjailConfigBuilder.__init__`
- [x] 2.2 Store `skills_dir` as `os.path.abspath(skills_dir)` if provided
- [x] 2.3 In `build()`, emit a read-only mount entry for `skills_dir` when `os.path.isdir()` is true AND `os.path.commonpath([skills_dir, project_dir]) != project_dir` (skip nested under project dir)
- [x] 2.4 Add debug log when `skills_dir` is skipped (does not exist) and warning log when skipped because nested under project_dir
- [x] 2.5 Update `tests/test_nsjail_config.py` to assert the skills_dir mount is present when directory exists
- [x] 2.6 Add test in `tests/test_nsjail_config.py` for skipping mount when skills_dir is missing
- [x] 2.7 Add test in `tests/test_nsjail_config.py` for skipping mount when skills_dir is nested under project_dir

## 3. Builtin Executor — Wire allow_net and skills_dir

- [x] 3.1 Replace `shell_nsjail_network` constructor parameter with `allow_net: bool = False`
- [x] 3.2 Store `self._allow_net = allow_net` (replace `self._shell_nsjail_network`)
- [x] 3.3 Update `NsjailConfigBuilder` instantiation in `builtin_executor.py`: change `network=shell_nsjail_network` to `allow_net=self._allow_net`, and add `skills_dir=skills_dir`
- [x] 3.4 In `main.py`, resolve `skills_dir` to an absolute path relative to `_AGENT_DIR` before passing to `BuiltinExecutor` (e.g. `os.path.join(_AGENT_DIR, skills_dir)`)
- [x] 3.5 Update `main.py` wiring: pass `allow_net=bool(agent_cfg.get("allow_net", False))` and `skills_dir=skills_dir` to `BuiltinExecutor`
- [x] 3.6 In `main.py` config extraction, replace `shell_nsjail_network = agent_cfg.get("shell_nsjail_network", "none")` with `allow_net = bool(agent_cfg.get("allow_net", False))`

## 4. Shell Tool — Update confirmation logic

- [x] 4.1 In `builtin_tools/shell.py` `_should_confirm()`, replace `self._owner._shell_nsjail_network == "none"` with `not self._owner._allow_net`
- [x] 4.2 Update inline docstring/comments to reference `allow_net` instead of `shell_nsjail_network`

## 5. E2E Mount Tests

- [x] 5.1 Update `tests/nsjail/test_nsjail_mounts.py` to verify `skills_dir` is mounted read-only inside the jail
- [x] 5.2 Add E2E test: skill script is executable inside the jail when `skills_dir` exists
- [x] 5.3 Add E2E test: writing to `skills_dir` inside the jail fails with permission error
- [x] 5.4 Verify `shell_logs` is NOT mounted inside the jail (negative test)

## 6. Vulture Whitelist

- [x] 6.1 Run `vulture . vulture_whitelist.py --min-confidence 80` and add any new flagged public API symbols to `vulture_whitelist.py`
- [x] 6.2 Ensure removed symbols (`shell_nsjail_network`) are cleaned from whitelist if present

## 7. Validation & Lint

- [x] 7.1 Run `make check` (ruff + vulture + pytest) and fix all failures
- [x] 7.2 Run `openspec validate add-shell-isolation-improvements --type change --strict` before archive

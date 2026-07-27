## 1. Config schema

- [ ] 1.1 Add `nsjail_state_dir: str = ""` field to `AgentConfig` in `config_schema.py`

## 2. NsjailConfigBuilder: XDG state path and agent-dir blocklist

- [ ] 2.1 Add `nsjail_state_dir: str = ""` and `agent_dir: str = ""` constructor params to `NsjailConfigBuilder` in `nsjail_config.py`
- [ ] 2.2 Change `_load_trusted_mounts` to read `trusted_dirs.json` from `nsjail_state_dir` instead of `data_dir`; create the dir with `os.makedirs(..., exist_ok=True)` on first use
- [ ] 2.3 Extend the per-call reject set in `_load_trusted_mounts` with `os.path.realpath(agent_dir)` and `os.path.realpath(nsjail_state_dir)` (and its XDG parent) alongside `_BLOCKED_SYSTEM_PREFIXES`

## 3. BuiltinExecutor wiring

- [ ] 3.1 Add `nsjail_state_dir: str = ""` and `agent_dir: str = ""` params to `BuiltinExecutor.__init__` in `builtin_executor.py`
- [ ] 3.2 Forward both params to `NsjailConfigBuilder` construction in `BuiltinExecutor`

## 4. Trusted-dir commands path update

- [ ] 4.1 In `builtin_tools/files.py`, update the `/dir add`, `/dir remove`, and `/dir list` handlers to read/write `trusted_dirs.json` from `_nsjail_state_dir` (passed via `BuiltinExecutor`) instead of `data_dir`

## 5. Composition root

- [ ] 5.1 In `main.py`, compute `nsjail_state_dir` from `XDG_STATE_HOME` (default `~/.local/state`) + agent name + `"nsjail"`, honouring `app_cfg.agent.nsjail_state_dir` override
- [ ] 5.2 Pass `agent_dir=_AGENT_DIR` and `nsjail_state_dir=nsjail_state_dir` to `BuiltinExecutor(...)`

## 6. Tests

- [ ] 6.1 In `tests/test_nsjail_config.py`, update fixtures to pass `nsjail_state_dir` and `agent_dir` to `NsjailConfigBuilder`
- [ ] 6.2 Add test: entry in `trusted_dirs.json` pointing to `agent_dir` is rejected by `_load_trusted_mounts`
- [ ] 6.3 Add test: entry pointing to `nsjail_state_dir` is rejected
- [ ] 6.4 Add test: `trusted_dirs.json` is read from `nsjail_state_dir`, not `data_dir`
- [ ] 6.5 Run `make check` and confirm all tests pass

## 7. Validation

- [ ] 7.1 Run `openspec validate nsjail-xdg-state-isolation --type change --strict` before archiving

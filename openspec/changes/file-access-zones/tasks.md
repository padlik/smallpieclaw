## 1. Config: workspace_dir field

- [x] 1.1 Add `workspace_dir: str = "~/Documents"` field to `PathsConfig` in `config_schema.py`
- [x] 1.2 Add `workspace_dir` to `vulture_whitelist.py` if vulture flags it as unused
- [x] 1.3 Run `make lint` to verify no ruff/vulture regressions

## 2. TrustedZoneChecker core

- [ ] 2.1 Update `builtin_tools/access_control.py` with `ZoneClassification` enum (`TRUSTED`, `REQUEST_GRANT`, `UNRECOGNISED`) and `TrustedDir` dataclass (`path`, `added`, `mode`); derive allow/confirm action from zone: TRUSTED/REQUEST_GRANT → allow; UNRECOGNISED → confirm; remove `INTERNAL` zone entirely
- [ ] 2.2 Update `TrustedZoneChecker.__init__`: resolve only default trusted dirs (`workspace_dir`, `downloads_dir`, `tmp_dir`) via `os.path.expanduser()` + `os.path.realpath()` from `PathsConfig`; load user-added dirs from `data/trusted_dirs.json` (handle missing file gracefully); no internal dirs; accept `vault_path: str` parameter and store the resolved real paths of `data/trusted_dirs.json` and the vault file (from `vault_path`, honoring `$SPC_VAULT_FILE`) as explicit UNRECOGNISED overrides (see 2.3)
- [ ] 2.3 Update `TrustedZoneChecker.classify(path: str, operation: str = "write", request_grants: frozenset[str] = frozenset()) -> ZoneClassification`: default `"write"` is fail-safe; check UNRECOGNISED overrides first (trust-store path + vault file path are always UNRECOGNISED regardless of whether a parent is trusted); then check zones in priority order (trusted → request_grant → unrecognised); use `os.path.realpath()` for all comparisons; use separator-boundary containment; for `"r"`-mode trusted dirs, return UNRECOGNISED when `operation == "write"`
- [x] 2.4 Implement `GrantTracker.add(path: str)`: store `os.path.dirname(realpath(path))` in in-memory set (grants parent dir of path)
- [x] 2.5 Implement `GrantTracker.reset()`: clear in-memory grant set; `GrantTracker.snapshot() -> frozenset[str]` returns a thread-safe copy passed to `classify()`
- [ ] 2.6 Update `TrustedZoneChecker.add_trusted(path: str, mode: str = "rw") -> None`: persist entry with `mode` field to `data/trusted_dirs.json` via `_atomic_save_json()`
- [x] 2.7 Implement `TrustedZoneChecker.remove_trusted(index: int) -> str`: remove by 1-based index from user-added list, persist, return removed path; raise `IndexError` for invalid index
- [x] 2.8 Implement `TrustedZoneChecker.list_user_trusted() -> list[TrustedDir]`: return user-added list sorted by path
- [ ] 2.9 Add `access_control` to `builtin_tools/__init__.py` exports if applicable; add any new public symbols to `vulture_whitelist.py`; remove `is_write_protected_internal` from whitelist and from `access_control.py`
- [x] 2.10 ONE `TrustedZoneChecker` instance is shared between main agent and sub-agents (persistent trust store is read-only after construction); request-grant isolation is provided by a per-`BuiltinExecutor` `GrantTracker` (not by separate checker instances)

## 3. file_* tool confirmation logic

- [ ] 3.1 Ensure `BuiltinExecutor` receives `trusted_zone_checker` attribute (injected from `main.py`); `files.py` reads it via `self._owner.trusted_zone_checker`
- [ ] 3.2 Update `_exec_file_write`: pass `operation="write"` to `classify()`; confirm if UNRECOGNISED; remove all INTERNAL-zone special cases; sensitive-pattern check stacks on top
- [ ] 3.3 Update `_exec_file_patch`: same as 3.2
- [ ] 3.4 Update `_exec_file_read`: pass `operation="read"` to `classify()`; confirm if UNRECOGNISED; remove INTERNAL auto-allow; sensitive patterns remain an additional gate
- [ ] 3.5 Run `make lint` after file changes
- [ ] 3.6 Update `_exec_file_diff`/`_run_file_diff`: pass `operation="read"` to classify for both path_a and path_b; confirm if either is UNRECOGNISED; apply `_is_sensitive_path()` check after zone classification
- [ ] 3.7 Update `_exec_file_send`/`_run_file_send`: pass `operation="read"` to classify; confirm if UNRECOGNISED; apply `_is_sensitive_path()` check after zone classification

## 4. ReactContext and react_loop integration

- [x] 4.1 `ReactContext` dataclass in `react_loop.py` has `trusted_zone_checker: TrustedZoneChecker` and `grant_tracker: Optional[GrantTracker]` fields; verify both present
- [x] 4.2 Call `ctx.grant_tracker.reset()` at the start of `react_loop()`, before the tool dispatch loop
- [ ] 4.3 Update `main.py`: construct ONE `TrustedZoneChecker(paths_config=app_cfg.paths, data_dir=..., vault_path=vault_path(cfg))` — pass the resolved vault path from `vault_path(cfg)` so `$SPC_VAULT_FILE` overrides are honoured; inject same instance into `BuiltinExecutor.trusted_zone_checker` AND `ReactContext`; also inject `builtin_executor.grant_tracker` into `ReactContext.grant_tracker`

## 5. Telegram: extended confirmation buttons

- [x] 5.1 Add `[Allow this request]` inline button to out-of-zone confirmation prompt in `telegram_callbacks.py`; on tap, call `grant_tracker.add(path)` and allow the staged operation
- [x] 5.2 Add `[Add to trusted]` inline button to same prompt; on tap, call `checker.add_trusted(os.path.dirname(realpath(path)))` and allow the staged operation
- [x] 5.3 Ensure both new buttons are only shown for out-of-zone prompts (not sensitive-pattern-only prompts inside trusted zones)

## 6. Telegram: /dir command

- [x] 6.1 Add `/dir` handler in `telegram_commands.py` that dispatches to `list` or `del N` sub-commands
- [ ] 6.2 Update `/dir list`: call `checker.list_user_trusted()`; format as numbered list sorted by path with mode annotation (`[rw]`/`[r]`); return empty-state message if list is empty
- [x] 6.3 Implement `/dir del N`: parse N, call `checker.remove_trusted(N)`; reply with `Removed: <path>`; reply with error message for invalid N
- [x] 6.4 Register `/dir` handler in Telegram interface startup (do NOT add to public command discovery menu — it is an operator command)

## 7. Tests

- [ ] 7.1 Update unit tests for `TrustedZoneChecker.classify()`: default trusted (rw), user-added trusted (rw), user-added trusted (r) with write operation, request grant, unrecognised (including agent-internal paths), symlink outside trusted zone, `..` traversal attempt; trust-store and vault remain UNRECOGNISED even when their parent dir is added to trusted; remove any INTERNAL-zone test cases
- [x] 7.1b Write unit test for sibling-prefix containment: /srv/shared-evil is NOT contained in /srv/shared
- [x] 7.2 Write unit tests for `GrantTracker.reset()`: grant active during request, cleared after reset
- [ ] 7.3 Update unit tests for `add_trusted()` / `remove_trusted()` / `list_user_trusted()`: persistence round-trip with `mode` field, invalid index, missing file on load, backward-compat with entries missing `mode`
- [ ] 7.4 Update unit tests for `file_*` confirmation logic: trusted zone rw (no confirm), trusted zone r with write (confirm staged), unrecognised zone including agent-internal path (confirm staged), sensitive pattern in trusted zone (confirm staged)
- [ ] 7.5 Run `make test` and verify all tests pass

## 8. Validation

- [ ] 8.1 Run `make check` (lint + tests) and confirm clean
- [ ] 8.2 Run `openspec validate file-access-zones --type change --strict` and confirm no spec violations

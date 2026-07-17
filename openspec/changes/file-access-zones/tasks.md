## 1. Config: workspace_dir field

- [x] 1.1 Add `workspace_dir: str = "~/Documents"` field to `PathsConfig` in `config_schema.py`
- [x] 1.2 Add `workspace_dir` to `vulture_whitelist.py` if vulture flags it as unused
- [x] 1.3 Run `make lint` to verify no ruff/vulture regressions

## 2. TrustedZoneChecker core

- [x] 2.1 Create `builtin_tools/access_control.py` with `ZoneClassification` enum (`INTERNAL`, `TRUSTED`, `REQUEST_GRANT`, `UNRECOGNISED`) and `TrustedDir` dataclass; derive allow/confirm action from zone: INTERNAL/TRUSTED/REQUEST_GRANT → allow; UNRECOGNISED → confirm
- [x] 2.2 Implement `TrustedZoneChecker.__init__`: resolve internal dirs and default trusted dirs via `os.path.realpath()` from `PathsConfig`; load user-added dirs from `data/trusted_dirs.json` (handle missing file gracefully)
- [x] 2.3 Implement `TrustedZoneChecker.classify(path: str) -> ZoneClassification`: check zones in priority order (internal → trusted → request_grant → unrecognised); use `os.path.realpath()` for all comparisons; use separator-boundary containment (`path == zone_dir or path.startswith(zone_dir + os.sep)`); explicitly exclude `data/trusted_dirs.json` from INTERNAL auto-allow
- [x] 2.4 Implement `TrustedZoneChecker.grant_for_request(path: str)`: store `os.path.dirname(realpath(path))` in in-memory set
- [x] 2.5 Implement `TrustedZoneChecker.reset_request_grants()`: clear in-memory grant set
- [x] 2.6 Implement `TrustedZoneChecker.add_trusted(path: str)`: append to user-added list and atomically persist to `data/trusted_dirs.json` via `_atomic_save_json()`
- [x] 2.7 Implement `TrustedZoneChecker.remove_trusted(index: int) -> str`: remove by 1-based index from user-added list, persist, return removed path; raise `IndexError` for invalid index
- [x] 2.8 Implement `TrustedZoneChecker.list_user_trusted() -> list[TrustedDir]`: return user-added list sorted by path
- [x] 2.9 Add `access_control` to `builtin_tools/__init__.py` exports if applicable; add any new public symbols to `vulture_whitelist.py`
- [x] 2.10 Each agent construction path creates a new TrustedZoneChecker instance (not shared between main agent and sub-agents)

## 3. file_* tool confirmation logic

- [x] 3.1 Ensure `BuiltinExecutor` receives `trusted_zone_checker` attribute (injected from `main.py`); `files.py` reads it via `self._owner.trusted_zone_checker`
- [x] 3.2 Update `_exec_file_write`: replace unconditional confirmation with `checker.classify(path) == ZoneClassification.UNRECOGNISED` guard; sensitive-pattern check stacks on top
- [x] 3.3 Update `_exec_file_patch`: same replacement as 3.2
- [x] 3.4 Update `_exec_file_read`: replace sensitive-only confirmation with zone check (`UNRECOGNISED`) — sensitive patterns remain an additional gate, not a replacement
- [x] 3.5 Run `make lint` after file changes
- [x] 3.6 Apply zone check to `file_diff`: classify both path_a and path_b; if either is UNRECOGNISED, stage confirmation before executing; apply `_is_sensitive_path()` check after zone classification (same stacking rule as file_read)
- [x] 3.7 Apply zone check to `file_send`: classify path; if UNRECOGNISED, stage confirmation before read/send; apply `_is_sensitive_path()` check after zone classification (same stacking rule as file_read)

## 4. ReactContext and react_loop integration

- [x] 4.1 Add `trusted_zone_checker: TrustedZoneChecker` field to `ReactContext` dataclass in `react_loop.py`
- [x] 4.2 Call `ctx.trusted_zone_checker.reset_request_grants()` at the start of `react_loop()`, before the tool dispatch loop
- [x] 4.3 Update `main.py`: construct `TrustedZoneChecker(paths_config=app_cfg.paths, data_dir=..., agent_name=...)` and inject into `ReactContext`; construct one `TrustedZoneChecker` per agent instance; inject same instance into `BuiltinExecutor` (attribute) AND `ReactContext` (field); sub-agent construction path must also create its own instance

## 5. Telegram: extended confirmation buttons

- [x] 5.1 Add `[Allow this request]` inline button to out-of-zone confirmation prompt in `telegram_callbacks.py`; on tap, call `checker.grant_for_request(path)` and allow the staged operation
- [x] 5.2 Add `[Add to trusted]` inline button to same prompt; on tap, call `checker.add_trusted(os.path.dirname(realpath(path)))` and allow the staged operation
- [x] 5.3 Ensure both new buttons are only shown for out-of-zone prompts (not sensitive-pattern-only prompts inside trusted zones)

## 6. Telegram: /dir command

- [x] 6.1 Add `/dir` handler in `telegram_commands.py` that dispatches to `list` or `del N` sub-commands
- [x] 6.2 Implement `/dir list`: call `checker.list_user_trusted()`; format as numbered list sorted by path; return empty-state message if list is empty
- [x] 6.3 Implement `/dir del N`: parse N, call `checker.remove_trusted(N)`; reply with `Removed: <path>`; reply with error message for invalid N
- [x] 6.4 Register `/dir` handler in Telegram interface startup (do NOT add to public command discovery menu — it is an operator command)

## 7. Tests

- [x] 7.1 Write unit tests for `TrustedZoneChecker.classify()`: internal path, default trusted, user-added trusted, request grant, unrecognised, symlink outside trusted zone, `..` traversal attempt
- [x] 7.1b Write unit test for sibling-prefix containment: /srv/shared-evil is NOT contained in /srv/shared
- [x] 7.2 Write unit tests for `reset_request_grants()`: grant active during request, cleared after reset
- [x] 7.3 Write unit tests for `add_trusted()` / `remove_trusted()` / `list_user_trusted()`: persistence round-trip, invalid index, missing file on load
- [x] 7.4 Write unit tests for updated `file_write` confirmation logic: trusted zone (no confirm), unrecognised zone (confirm staged), sensitive pattern in trusted zone (confirm staged)
- [x] 7.5 Run `make test` and verify all tests pass

## 8. Validation

- [x] 8.1 Run `make check` (lint + tests) and confirm clean
- [x] 8.2 Run `openspec validate file-access-zones --type change --strict` and confirm no spec violations

## 1. Remove `paths.tmp_dir` config override (D0)

- [x] 1.1 Remove the `tmp_dir` field from `PathsConfig` in `config_schema.py`, along with its parsing/mirroring logic (the `_expand_path(section.get("tmp_dir", ""))` block and its `raw["paths"]` mirror-back).
- [x] 1.2 Remove the `paths.tmp_dir` fallback branch in `builtin_tools/access_control.py`'s `TrustedZoneChecker` — it must always compute `f"/tmp/{agent_name}"`, with no `paths_config.tmp_dir` check.
- [x] 1.3 Remove the `paths.tmp_dir` example/documentation from `config.toml.example` and `README.md`.
- [x] 1.4 Update `tests/test_config_schema.py` to remove/adjust any test that constructs `PathsConfig` with a `tmp_dir` argument or asserts on the removed field.
- [x] 1.5 Update `tests/test_access_control.py` to remove/adjust any test that relies on the `paths.tmp_dir` override fallback in `TrustedZoneChecker`.

## 2. Unify `tmp_dir` resolution in `main.py` (D0, D2)

- [x] 2.1 Delete `main.py`'s existing cwd-basename `tmp_dir` computation (`main.py:281-282`).
- [x] 2.2 Replace it with `tmp_dir = f"/tmp/{app_cfg.agent.agent_name}"`, computed once at startup, before `os.makedirs`/`BuiltinExecutor` construction.
- [x] 2.3 Confirm `main.py`'s existing `TMPDIR`/`TMP`/`TEMP` process-env-var assignment (`main.py:375-377`) and its `os.makedirs` call still use this same `tmp_dir` value (no separate computation).
- [x] 2.4 Verify `BuiltinExecutor`'s own `agent_name` parameter (used for nsjail state dir / `trusted_dirs.json` / conversation store) is never used to derive `tmp_dir` anywhere.
- [x] 2.5 (D5) Delete `main.py`'s separate cwd-basename `agent_name` computation entirely — `_run()`'s `agent_name` parameter is now always `app_cfg.agent.agent_name`, consumed identically by `nsjail_state_dir`, `trusted_dirs_path`, `conversation_id`/`conversations_dir`, vault migration paths, `BuiltinExecutor`'s `agent_name` param, and `TrustedZoneChecker`'s `agent_name` param.

## 3. Thread `tmp_dir` into the nsjail config builder (D1, D2, D3)

- [x] 3.1 Add a `tmp_dir` constructor parameter to `NsjailConfigBuilder.__init__` in `nsjail_config.py`.
- [x] 3.2 In `build()`, emit the new mount immediately after the existing per-session `/tmp` scratch mount: `is_bind: true`, `rw: true`, `mandatory: true`, `src == dst == tmp_dir`.
- [x] 3.3 Update `builtin_executor.py` to accept `tmp_dir` as an already-resolved constructor argument (mirroring how `nsjail_session_tmpdir` is already threaded) and pass it straight to `NsjailConfigBuilder` — never re-derive it from `BuiltinExecutor`'s own `agent_name` parameter.
- [x] 3.4 Wire `main.py`'s `tmp_dir` (from task 2.2) through to `BuiltinExecutor`'s new parameter.
- [x] 3.5 (code-review fix) In `nsjail_config.py`'s `_load_trusted_mounts()`, skip `trusted_dirs.json` entries nested under `tmp_dir`, mirroring the existing `session_tmpdir` skip, so a stray trust-store entry under `tmp_dir` can't produce a duplicate/conflicting mount stanza for the same destination.
- [x] 3.6 (code-review fix) In `builtin_executor.py`, require `tmp_dir` truthy (alongside `nsjail_session_tmpdir`) before activating the nsjail backend, so a caller that omits `tmp_dir` gets no nsjail backend instead of a broken empty-path mandatory mount.

## 4. Add `TMPDIR`/`TMP`/`TEMP` base envars to the jail (D4)

- [x] 4.1 In `nsjail_config.py`'s `build()`, add `TMPDIR`, `TMP`, `TEMP` to the unconditionally-injected base envar block (alongside `PATH`/`HOME`/`LANG`/`TERM`), all set to `/tmp`.
- [x] 4.2 Confirm session env vars set via `shell_env_set` still override these via `-E` flags, same as existing base envars (no special-casing needed).

## 5. Tests

- [x] 5.1 Add/update `nsjail_config.py` unit tests: mount ordering (`/tmp` scratch before `tmp_dir`), mount attributes (`is_bind`, `rw`, `mandatory` all `true`, `src == dst == tmp_dir`), and the three new base envars.
- [x] 5.2 Add a VM-backed test (or extend an existing one) in `tests/nsjail/` verifying: a file written inside the jail under `tmp_dir` is readable via `file_read` outside; a file written via `file_write` outside is readable inside the jail; a missing `tmp_dir` at shell-call time fails the call with a non-zero exit rather than degrading; `shell("echo $TMPDIR $TMP $TEMP")` inside the jail returns `/tmp /tmp /tmp`; and a `shell_env_set("TMPDIR", ...)` override wins over the config envar.
- [x] 5.3 Run `make check` (ruff + vulture + pytest) and fix any fallout from the removed `paths.tmp_dir` field.

## 6. Validation

- [x] 6.1 Run `openspec validate fix-nsjail-tmp-handoff --type change --strict` before archive.

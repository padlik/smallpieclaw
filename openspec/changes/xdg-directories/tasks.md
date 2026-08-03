## 0. Branch setup

- [x] 0.1 Create feature branch `feature/xdg-directories` from `main`

---

## 1. `xdg.py` — new module + test fixture

- [x] 1.1 Create `xdg.py`; define `XDGPaths` frozen dataclass with all 20 fields matching the XDG Path Table in design.md (`config_home`, `data_home`, `state_home`, `cache_home`, `runtime_dir`, `config_file`, `scheduler_config`, `memory_file`, `graph_memory_db`, `tool_index_file`, `pid_file`, `secrets_file`, `logs_dir`, `log_file`, `log_jsonl`, `skills_dir`, `scheduler_state`, `scheduler_commands`, `scheduler_jobs`, `job_execution_log`)
- [x] 1.2 Implement `xdg_paths(agent_name: str) -> XDGPaths` — reads XDG env vars with spec-compliant fallbacks; `$XDG_RUNTIME_DIR` fallback is state_home when unset; function is pure and side-effect-free
- [x] 1.3 Implement `migration_sentinel_exists(paths: XDGPaths) -> bool` (glob `migrated_from_*.sentinel`) and `write_migration_sentinel(paths: XDGPaths) -> None` (timestamped UTC sentinel file in `state_home`)
- [x] 1.4 Write `tests/test_xdg.py`: default resolved paths for all 20 fields, each XDG env var override, `XDG_RUNTIME_DIR` set vs. unset, purity (no dirs created), sentinel round-trip (write → exists → True), sentinel absent → False
- [x] 1.5 Add `tmp_xdg` fixture to `tests/conftest.py` — monkeypatches all five `XDG_*` env vars to `tmp_path` subdirs (`config`, `data`, `state`, `cache`, `runtime`) and returns `tmp_path`; all subsequent tests use this fixture instead of writing to real home

---

## 2. `config_schema.py` — field removal

- [x] 2.1 Remove 8 `PathsConfig` fields: `data_dir`, `tool_index_file`, `memory_file`, `pid_file`, `downloads_dir`, `log_file`, `file_vault`, `skills_dir`
- [x] 2.2 Remove `GraphMemoryConfig.db_path`; remove `AgentConfig.agent_home`
- [x] 2.3 Remove `SPC_VAULT_FILE` and `SPC_LOG_DIR` env var override logic; remove `vault_path()`, `log_path()`, and `log_dir()` helpers
- [x] 2.4 Update `_parse_paths()` to parse only `workspace_dir` (default `~/Documents`); all other path derivation removed
- [x] 2.5 Remove vulture whitelist entries for the symbols deleted in tasks 2.1–2.4 only; do NOT add `XDGPaths` field entries yet — those fields have no callers until sections 7–9 are complete; full vulture fixup is deferred to task 11.2

---

## 3. `main.py` — startup flow refactor

- [x] 3.1 Add `--agent-name <name>` required argparse argument; remove `_AGENT_DIR` module-level constant and all references to it
- [x] 3.2 Implement `_create_xdg_dirs(paths: XDGPaths)` — `parents=False, exist_ok=True` for `runtime_dir`; `parents=True, exist_ok=True` for all other dirs (`config_home`, `data_home`, `state_home`, `cache_home`, `logs_dir`, `skills_dir`)
- [x] 3.3 Implement `_warn_relative_paths(cfg: dict)` — scan all string values; emit `logger.warning(...)` (structlog) for any value starting with `.`; values starting with `~` or `/` are valid and not warned on
- [x] 3.4 Implement `_check_migration(paths: XDGPaths, agent_name: str)` — if sentinel exists: return; if `Path(__file__).parent / "config.toml"` exists: call `migrate.main(agent_name, source=Path(__file__).parent)`; log results at INFO *(note: complete task 4.1 before implementing the `migrate.main()` call — `migrate.py` does not exist until section 4)*
- [x] 3.5 Wire new startup sequence: (1) `agent_name = args.agent_name`, (2) `paths = xdg_paths(agent_name)`, (3) `_create_xdg_dirs(paths)`, (4) `_check_migration(paths, agent_name)`, (5) exit with message if `paths.config_file` missing, (6) `cfg = load_config(paths.config_file)`, (7) `_warn_relative_paths(cfg)`, (8) existing startup continues with `paths` passed to `_run()`
- [x] 3.6 Derive `downloads_dir = Path(cfg["paths"].get("workspace_dir", "~/Documents")).expanduser() / "downloads"` in startup; pass to constructors that previously received `downloads_dir`

---

## 4. `migrate.py` — new script

- [x] 4.1 Create `migrate.py` with CLI: `--agent-name` (required), `--source` (default: `Path(__file__).parent`), `--dry-run`; expose `main(agent_name, source, dry_run=False)` callable from `_check_migration`
- [x] 4.2 Implement detection: old layout present if `<source>/config.toml` exists AND `migration_sentinel_exists(paths)` is False; exit 0 silently if sentinel already exists
- [x] 4.3 Implement all 10 migration steps in order (design.md migration table): copy `config.toml`, `scheduler.toml`, `data/memory.json`, `data/graph_memory*` (glob all three variants), `data/scheduler_state.json`, `data/scheduler_commands.json`, `data/scheduler_jobs.json`, `data/job_execution_log.jsonl`, `skills/` (recursive); each copy uses `shutil.copy2`, writes to `<dest>.tmp` then renames, skips if destination already exists
- [x] 4.4 Delete `<source>/data/tool_index.json` after all other steps succeed (not migrated; regeneratable); do nothing if it doesn't exist
- [x] 4.5 Call `write_migration_sentinel(paths)` after all steps succeed; skip sentinel under `--dry-run`; print summary of what was copied/skipped/deleted

---

## 5. Tests for `migrate.py`

- [x] 5.1 Write `tests/test_migrate.py` using `tmp_xdg` fixture: detection (old layout present), detection (no `config.toml` → skips), detection (sentinel exists → skips)
- [x] 5.2 Test each migration step: file copied to correct XDG destination, skip-if-dest-exists, `graph_memory*` glob copies all three variants; test `--source` pointing to a non-default directory — source files read from given path, XDG destinations resolve under agent namespace
- [x] 5.3 Test `tool_index.json` deleted from source after all steps succeed; not deleted if other steps fail
- [x] 5.4 Test sentinel written after success; not written under `--dry-run`; dry-run prints actions without writing

---

## 6. Tests for `main.py` startup flow

- [x] 6.1 Write `tests/test_main.py` using `tmp_xdg` + `monkeypatch`: `--agent-name` absent → non-zero exit; `--agent-name` present → `xdg_paths()` called with correct name; first launch creates all XDG dirs; second launch is idempotent (no errors); `runtime_dir` parent missing → `FileNotFoundError`
- [x] 6.2 Test config-absent exit message contains full path to `paths.config_file`; `_warn_relative_paths` — value starting with `.` emits structured warning, value starting with `~/` does not; default `workspace_dir` produces `~/Documents/downloads`, custom `workspace_dir` produces `<workspace>/downloads`; `_check_migration` auto-triggers when `config.toml` is alongside `main.py` and no sentinel exists; `_check_migration` skips immediately when sentinel already exists

---

## 7. `scheduler.py` — update to `XDGPaths`

- [x] 7.1 Update `Scheduler.__init__` to accept `paths: XDGPaths` instead of `data_dir: str`; replace all 4 path constructions with `paths.scheduler_state`, `paths.scheduler_commands`, `paths.scheduler_jobs`, `paths.job_execution_log`
- [x] 7.2 Update `main.py` (and any other callers) to pass `paths` to `Scheduler`

---

## 8. Update storage callers — memory, tool index, graph memory

- [x] 8.1 Update `memory_store.py` (and callers) to accept `memory_path: Path` directly; remove `data_dir`-based path construction
- [x] 8.2 Update `tool_index.py` (and callers) to accept `index_path: Path` directly
- [x] 8.3 Update `graph_memory.py` (and callers) to accept and use `db_path: Path` directly (`GraphMemoryConfig.db_path` was already removed in task 2.2)

---

## 9. Update storage callers — logging, skills, nsjail, pid

- [x] 9.1 Update `agent_logging.py` setup (and callers) to receive `logs_dir: Path`, `log_file: Path`, `log_jsonl: Path` from `XDGPaths` instead of deriving from `log_dir()` or config
- [x] 9.2 Update `skill_registry.py` (and callers) to accept `skills_dir: Path` directly
- [x] 9.3 Update callers of `nsjail_config.py` to pass `str(paths.skills_dir)` for the skills bind-mount entry
- [x] 9.4 Implement read-only-mount exemption in `nsjail_config.py`: the `skills_dir` bind-mount must bypass the user-home prefix blocklist (`~/.local`, `~/.ssh`, `~/.config`, etc.) because it is mounted read-only (`rw: false`); add an exemption code-path so that `paths.skills_dir` under `~/.local/state/<name>/skills/` is accepted without triggering the blocklist
- [x] 9.5 Write tests for task 9.4: "skills_dir under a blocked user-home prefix is accepted when mounted read-only" — bind-mount entry is generated with `rw: false`; "skills_dir uses XDG-derived path" — mount uses `paths.skills_dir` not a project-relative path
- [x] 9.6 Update PID file usage in `main.py` to use `paths.pid_file`; update secrets/vault references to use `paths.secrets_file`

---

## 10. `tests/conftest.py` — remaining fixture migration

- [x] 10.1 Update all test files that reference `tmp_agent_dir` to use `tmp_xdg` + `xdg_paths("test-agent")` (the `tmp_xdg` fixture was added in task 1.5)
- [x] 10.2 Update tests that reference removed `PathsConfig` fields (`data_dir`, `memory_file`, etc.) or `AgentConfig.agent_home` to use `XDGPaths` fields

---

## 11. Lint, validate, and final checks

- [x] 11.1 Run `ruff check .` and fix any issues
- [x] 11.2 Run `vulture . vulture_whitelist.py --min-confidence 80` and update `vulture_whitelist.py` for all remaining gaps (including any `XDGPaths` fields still flagged now that all callers are wired)
- [x] 11.3 Run `make test` — all tests pass
- [x] 11.4 Run `openspec validate xdg-directories --type change --strict`

---

## 12. Follow-up: close remaining agent_home generated-content gap

Review after initial apply found three generated files still resolving relative to `agent_home`/cwd, missed by the original design's PathsConfig-removal list. Closes the gap so `agent_home` holds no generated content whatsoever.

- [x] 12.1 Remove `results_memory_file` and `longterm_memory_file` from `PathsConfig`; remove their parsing in `_parse_paths()`
- [x] 12.2 `main.py`: `results_path` resolves to `paths.data_home / "results_memory.json"`
- [x] 12.3 `backfill_graph_memory.py`: add `--agent-name`; `--longterm-path`/`--state-file` default to `xdg_paths(agent_name).data_home / "longterm_memory.json"` / `"graph_memory_backfill_state.json"` respectively (both remain CLI-overridable)
- [x] 12.4 `migrate.py`: add explicit copy steps for `results_memory.json`, `longterm_memory.json`, `graph_memory_backfill_state.json`; tighten the `graph_memory*` step from a glob to an explicit 3-item list (the glob was also matching `graph_memory_backfill_state.json` by prefix)
- [x] 12.5 Update `tests/test_migrate.py` for the 3 new migration steps
- [x] 12.6 Update specs (`agent-scoped-directories`, `xdg-data-migration`), `design.md`, and `proposal.md` to document the above (originally missed in review)
- [x] 12.7 Re-run `openspec validate xdg-directories --type change --strict` and full test suite

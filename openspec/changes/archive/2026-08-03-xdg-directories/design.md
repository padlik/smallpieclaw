## Overview

All agent storage is moved out of `agent_home` and into XDG Base Directory locations keyed by `agent_name`. A new `xdg.py` module is the single source of truth for path resolution. `main.py` gains a required `--agent-name` CLI argument and loses `_AGENT_DIR`. Nine `PathsConfig` fields and `GraphMemoryConfig.db_path` are removed from `config_schema.py`. A `migrate.py` script handles one-shot migration from the old layout.

---

## Module: `xdg.py` (new)

Central path resolver. Reads XDG env vars with XDG-spec fallbacks, computes all agent paths, returns an `XDGPaths` dataclass.

```python
@dataclass(frozen=True)
class XDGPaths:
    # Roots (one per XDG bucket)
    config_home: Path   # $XDG_CONFIG_HOME/<name>   → ~/.config/<name>
    data_home: Path     # $XDG_DATA_HOME/<name>      → ~/.local/share/<name>
    state_home: Path    # $XDG_STATE_HOME/<name>     → ~/.local/state/<name>
    cache_home: Path    # $XDG_CACHE_HOME/<name>     → ~/.cache/<name>
    runtime_dir: Path   # $XDG_RUNTIME_DIR/<name> or state_home if $XDG_RUNTIME_DIR unset

    # Derived leaf paths (consumed by callers)
    config_file: Path         # config_home / "config.toml"
    scheduler_config: Path    # config_home / "scheduler.toml"
    memory_file: Path         # data_home / "memory.json"
    graph_memory_db: Path     # data_home / "graph_memory"  ← DB base path (not a dir); LadybugDB creates .wal and .wal.checkpoint siblings
    tool_index_file: Path     # cache_home / "tool_index.json"
    pid_file: Path            # runtime_dir / "agent.pid"
    secrets_file: Path        # state_home / "secrets.toml"
    logs_dir: Path            # state_home / "logs"
    log_file: Path            # state_home / "logs" / "agent.log"
    log_jsonl: Path           # state_home / "logs" / "agent.jsonl"
    skills_dir: Path          # state_home / "skills"
    scheduler_state: Path     # state_home / "scheduler_state.json"
    scheduler_commands: Path  # state_home / "scheduler_commands.json"
    scheduler_jobs: Path      # state_home / "scheduler_jobs.json"
    job_execution_log: Path   # state_home / "job_execution_log.jsonl"
```

Note: `data_home` is the data directory (replaces old `data_dir`). No duplicate alias is provided — all callers use `paths.data_home` directly.

```python
def xdg_paths(agent_name: str) -> XDGPaths:
    """Resolve all XDG paths for agent_name. Reads env vars; never creates dirs."""
    xdg_config  = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    xdg_data    = Path(os.environ.get("XDG_DATA_HOME",   "~/.local/share")).expanduser()
    xdg_state   = Path(os.environ.get("XDG_STATE_HOME",  "~/.local/state")).expanduser()
    xdg_cache   = Path(os.environ.get("XDG_CACHE_HOME",  "~/.cache")).expanduser()
    runtime_env = os.environ.get("XDG_RUNTIME_DIR", "")
    # $XDG_RUNTIME_DIR is always absolute when set by systemd-logind; no expanduser needed
    xdg_runtime = Path(runtime_env) / agent_name if runtime_env else xdg_state / agent_name
    ...
```

`xdg_paths()` is pure and side-effect free. Directory creation happens exclusively in `main.py`.

### Migration sentinel helpers

```python
def migration_sentinel_exists(paths: XDGPaths) -> bool:
    """Return True if any migrated_from_*.sentinel file exists in state_home."""
    return any(paths.state_home.glob("migrated_from_*.sentinel"))

def write_migration_sentinel(paths: XDGPaths) -> None:
    """Write a new timestamped sentinel file."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (paths.state_home / f"migrated_from_{ts}.sentinel").write_text("")
```

---

## `main.py` Refactor

### CLI change

```python
parser = argparse.ArgumentParser()
parser.add_argument("--agent-name", required=True,
                    help="Agent name (required; resolves all XDG paths)")
```

No default. Omitting `--agent-name` is a startup error. `_AGENT_DIR` is **removed** entirely.

### Startup flow

```
1. Parse --agent-name → agent_name  (required; no default)
2. paths = xdg_paths(agent_name)
3. _create_xdg_dirs(paths)
4. _check_migration(paths, agent_name)
5. if not paths.config_file.exists():
       sys.exit(f"No config found. Create: {paths.config_file}")
6. cfg = load_config(paths.config_file)
7. _warn_relative_paths(cfg)
8. ... existing startup continues, paths passed to _run()
```

### `_create_xdg_dirs(paths)` — always called, idempotent

Creates: `config_home`, `data_home`, `state_home`, `cache_home`, `logs_dir`, `skills_dir`, and `runtime_dir` itself (the agent-scoped subdirectory; its parent `$XDG_RUNTIME_DIR` is owned by systemd-logind and must not be created here). Use `parents=False, exist_ok=True` for `runtime_dir`; `parents=True, exist_ok=True` for all other dirs.

### `_warn_relative_paths(cfg)` — startup validation

Scans all string values in `cfg`. Warns only on values that start with `.` — these are genuinely relative paths that may not resolve as expected. Values like `~/Documents` (starts with `~`) or `/abs/path` are valid and not warned on.

### Downloads path

```python
downloads_dir = Path(cfg["paths"].get("workspace_dir", "~/Documents")).expanduser() / "downloads"
```

`workspace_dir` is the one remaining user-configurable path in `[paths]`.

### Path passing

`_run()` receives `paths: XDGPaths`. All constructors updated to accept individual `Path` fields from `XDGPaths` (e.g., `memory_path=paths.memory_file`, `index_path=paths.tool_index_file`).

---

## `config_schema.py` Changes

### `PathsConfig` — removed fields

| Field | Was | Now |
|-------|-----|-----|
| `data_dir` | `str = "data"` | derived: `paths.data_home` |
| `tool_index_file` | `str = "data/tool_index.json"` | derived: `paths.tool_index_file` |
| `memory_file` | `str = "data/memory.json"` | derived: `paths.memory_file` |
| `pid_file` | `str = "data/agent.pid"` | derived: `paths.pid_file` |
| `downloads_dir` | `str = "downloads"` | derived: `Path(workspace_dir).expanduser() / "downloads"` |
| `log_file` | `str = "agent.log"` | derived: `paths.log_file` / `paths.log_jsonl` |
| `file_vault` | `str` | derived: `paths.secrets_file` |
| `skills_dir` | `str = "skills"` | derived: `paths.skills_dir` |
| `results_memory_file` | `str = "data/results_memory.json"` | derived: `paths.data_home / "results_memory.json"` |
| `longterm_memory_file` | `str = "data/longterm_memory.json"` | derived: `paths.data_home / "longterm_memory.json"` (legacy/backfill-only; overridable only via `backfill_graph_memory.py --longterm-path`) |

`workspace_dir` field **stays** — it is the one user-configurable path (default: `~/Documents`).

Note: `results_memory.json` and `longterm_memory.json` have no dedicated `XDGPaths` field — like `job_contexts/`, `prompts.jsonl`, and `strategies.json`, they are resolved ad hoc as `paths.data_home / "<filename>"` at the call site (`main.py`, `backfill_graph_memory.py`) rather than added to the frozen 20-field `XDGPaths` dataclass.

### `GraphMemoryConfig` — removed field

| Field | Was | Now |
|-------|-----|-----|
| `db_path` | `str = "data/graph_memory"` | derived: `paths.graph_memory_db` |

### `AgentConfig` — removed field

| Field | Was | Now |
|-------|-----|-----|
| `agent_home` | auto-derived from `agent_name` | retired; `agent_home` is no longer a concept |

### Env-var overrides retired

| Env var | Was | Now |
|---------|-----|-----|
| `SPC_VAULT_FILE` | overrode vault path in `vault_path()` | retired; vault is always `paths.secrets_file` |
| `SPC_LOG_DIR` | overrode log directory in `log_dir()` | retired; logs are always `paths.logs_dir` |

### Helpers removed

`vault_path()`, `log_path()`, and `log_dir()` are removed. Callers use `paths.secrets_file`, `paths.log_file`, `paths.log_jsonl`, and `paths.logs_dir`.

### `_parse_paths()` update

Only `workspace_dir` is parsed from `[paths]`. All other path derivation is handled by `xdg.py`.

---

## `migrate.py` (new script)

### CLI

```
python migrate.py --agent-name <name> [--source <agent_home_dir>] [--dry-run]
```

- `--agent-name` is required (no default). XDG target paths cannot be resolved without it.
- `--source` defaults to the directory containing `migrate.py`.
- `--dry-run` prints what would be copied/deleted without writing anything; **the sentinel is not written under `--dry-run`**.

### Detection

Old layout is present if `<source>/config.toml` exists AND `migration_sentinel_exists(paths)` is False.

### Migration steps (in order)

| Source | Destination | Notes |
|--------|-------------|-------|
| `<source>/config.toml` | `paths.config_file` | skip if dest exists |
| `<source>/scheduler.toml` | `paths.scheduler_config` | skip if dest exists |
| `<source>/data/memory.json` | `paths.memory_file` | skip if dest exists |
| `<source>/data/graph_memory`, `.wal`, `.wal.checkpoint` | `paths.data_home / <filename>` | explicit 3-item list (not a glob — see note below); each file copied to `paths.data_home / <filename>` |
| `<source>/data/scheduler_state.json` | `paths.scheduler_state` | skip if dest exists |
| `<source>/data/scheduler_commands.json` | `paths.scheduler_commands` | skip if dest exists |
| `<source>/data/scheduler_jobs.json` | `paths.scheduler_jobs` | skip if dest exists |
| `<source>/data/job_execution_log.jsonl` | `paths.job_execution_log` | skip if dest exists |
| `<source>/data/results_memory.json` | `paths.data_home / "results_memory.json"` | skip if dest exists |
| `<source>/data/longterm_memory.json` | `paths.data_home / "longterm_memory.json"` | skip if dest exists; legacy/backfill-only file |
| `<source>/data/graph_memory_backfill_state.json` | `paths.data_home / "graph_memory_backfill_state.json"` | skip if dest exists |
| `<source>/skills/` | `paths.skills_dir` | recursive copy; skip if dest exists |
| `<source>/data/tool_index.json` | *(deleted from source)* | regeneratable; **not copied**; source file removed after all other steps succeed |

All copy operations are non-destructive (source files preserved), except `tool_index.json` which is deleted from source but not copied (it regenerates on next run). Copy uses `shutil.copy2`. Writes to `<dest>.tmp` then renames.

The `data/graph_memory*` step uses an explicit three-item list (`graph_memory`, `graph_memory.wal`, `graph_memory.wal.checkpoint`), not a glob — a glob would also incorrectly match `graph_memory_backfill_state.json` by prefix, double-copying it via the wrong step.

Note: `secrets.toml` (vault file) is **not** in the migration table. It was already stored at `$XDG_STATE_HOME/<name>/secrets.toml` in the old layout — no migration step is needed.

### `backfill_graph_memory.py` changes

`--agent-name <name>` is added (default `"piclaw"`, for backward-compatible manual invocation). Defaults for `--longterm-path` and `--state-file` change from `[paths]`-config-derived / sibling-of-longterm-path to `xdg_paths(agent_name).data_home / "longterm_memory.json"` and `xdg_paths(agent_name).data_home / "graph_memory_backfill_state.json"` respectively. `--db-path` (already added) defaults to `xdg_paths(agent_name).graph_memory_db`. All three remain CLI-overridable for one-off manual runs against a different location; none are read from `config.toml`.

### Sentinel

After all steps succeed: `write_migration_sentinel(paths)`. On subsequent runs, `migration_sentinel_exists(paths)` → exit 0. Not written under `--dry-run`.

### Auto-trigger from `main.py`

`_check_migration(paths, agent_name)`:
1. If `migration_sentinel_exists(paths)` → return
2. If `Path(__file__).parent / "config.toml"` exists → run migration with `source=Path(__file__).parent`
3. Log results at INFO level

---

## `scheduler.py` Changes

`Scheduler.__init__` receives `paths: XDGPaths` instead of `data_dir: str`.

| Old | New |
|-----|-----|
| `os.path.join(data_dir, "scheduler_state.json")` | `paths.scheduler_state` |
| `os.path.join(data_dir, "scheduler_commands.json")` | `paths.scheduler_commands` |
| `os.path.join(data_dir, "scheduler_jobs.json")` | `paths.scheduler_jobs` |
| `os.path.join(data_dir, "job_execution_log.jsonl")` | `paths.job_execution_log` |

---

## `nsjail_config.py` Changes

`NsJailConfig` receives `skills_dir: str` (unchanged signature). Callers pass `str(paths.skills_dir)`. The bind-mount entry uses this path as both `src` and `dst`.

---

## `tests/conftest.py` Changes

### Remove: `tmp_agent_dir` fixture

### Add: `tmp_xdg` fixture

```python
@pytest.fixture
def tmp_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Override all XDG env vars to tmp_path subdirs. Tests never touch real home."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME",   str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME",  str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME",  str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    return tmp_path
```

Tests use `xdg_paths("test-agent")` after the fixture to get fully resolved paths.

---

## Systemd Unit Update

User services (`systemctl --user`) must add `--agent-name` to `ExecStart`. `$XDG_RUNTIME_DIR` is already set correctly by systemd-logind for user services.

```ini
# Before
ExecStart=/home/user/piclaw/venv/bin/python main.py

# After
ExecStart=/home/user/piclaw/venv/bin/python main.py --agent-name piclaw
```

`WorkingDirectory` remains pointing at `agent_home` (needed for the venv path) but no longer affects where any data is stored.

---

## Spec Impact Notes

### `agent-scoped-directories`

All of the following scenarios are **invalidated** and must be removed in the delta spec:

- "Default agent name and home" — describes `agent_home` resolution; `AgentConfig.agent_home` is retired
- "Custom agent name with default home" — same reason
- "Explicit agent home does NOT affect vault path" — `agent_home` concept retired; vault path is now always XDG-derived
- "Vault path overridden by environment variable" — uses `SPC_VAULT_FILE`; that env var is retired
- "Explicit absolute log_file overrides the default" — `log_file` config parameter removed
- "Explicit agent home does NOT affect log location" — GIVEN references `agent_home = "/opt/…"` in `[agent]`; `AgentConfig.agent_home` is retired
- "Vault migrated from old XDG_DATA_HOME location" — invalidated; vault (`secrets.toml`) was already at `$XDG_STATE_HOME/<name>/secrets.toml` in the old layout; no migration step exists or is needed; remove this scenario without replacement
- "Both old and new vault paths exist" — `migrate.py` uses skip-if-dest-exists; if `paths.secrets_file` already exists, migration leaves it untouched; no dual-path concept survives

The following scenarios must be **updated** (not removed):

- "Default log location for default agent" — references `[agent].agent_name` config field; clarify: `agent_name` is provided exclusively via `--agent-name` CLI arg and is not read from `[agent].agent_name` for path derivation; update GIVEN/THEN to reference `$XDG_STATE_HOME/<name>/logs/agent.log`
- "Custom agent name derives log location" — same; update to reflect CLI-sourced `agent_name` drives XDG path derivation
- "Logs are no longer written into the source checkout" — scenario remains valid; confirm: logs are at `paths.logs_dir` (`$XDG_STATE_HOME/<name>/logs/`); update any path references in GIVEN/THEN

**Rule text updates required:**

- Requirement 1 Rule (spec line ~14): references retired `agent_home` — must be rewritten to reference XDG-derived paths keyed by `agent_name`
- Requirement 2 Rule (spec line ~65): references both `agent_home` and the removed `log_file` override — must be rewritten; logs are always `paths.logs_dir`; no per-agent `log_file` override exists

The spec's Purpose line ("Define `agent_name` and `agent_home` configuration fields…") must be updated to: "Define `agent_name` as the sole agent identifier (resolved from `--agent-name` CLI); all storage paths derive from XDG Base Directories keyed by `agent_name`; `agent_home` is retired."

### `skills-dir-sandbox-mount`

The following scenarios are invalidated and must be removed or replaced in the delta spec:

- "skills_dir uses the configured path" — depends on `skills_dir` being a user-settable config parameter in `[paths]`; that parameter is removed
- "Skill script is executable inside the jail" — references `skills_dir` as "configured (default: `"skills"`, resolved relative to the agent home)"; that description no longer applies

Replacement: both scenarios should describe the XDG-derived path `$XDG_STATE_HOME/<agent_name>/skills/`.

---

## Full XDG Path Table (reference)

| File / Dir | XDG Var | Default Resolved Path | `XDGPaths` field |
|---|---|---|---|
| `config.toml` | `$XDG_CONFIG_HOME` | `~/.config/<name>/config.toml` | `config_file` |
| `scheduler.toml` | `$XDG_CONFIG_HOME` | `~/.config/<name>/scheduler.toml` | `scheduler_config` |
| `memory.json` | `$XDG_DATA_HOME` | `~/.local/share/<name>/memory.json` | `memory_file` |
| `graph_memory` (DB base) | `$XDG_DATA_HOME` | `~/.local/share/<name>/graph_memory` | `graph_memory_db` |
| `tool_index.json` | `$XDG_CACHE_HOME` | `~/.cache/<name>/tool_index.json` | `tool_index_file` |
| `agent.pid` | `$XDG_RUNTIME_DIR` | `/run/user/<uid>/<name>/agent.pid` | `pid_file` |
| `agent.pid` (fallback) | `$XDG_STATE_HOME` | `~/.local/state/<name>/agent.pid` | `pid_file` |
| `secrets.toml` | `$XDG_STATE_HOME` | `~/.local/state/<name>/secrets.toml` | `secrets_file` |
| `logs/` | `$XDG_STATE_HOME` | `~/.local/state/<name>/logs/` | `logs_dir` |
| `logs/agent.log` | `$XDG_STATE_HOME` | `~/.local/state/<name>/logs/agent.log` | `log_file` |
| `logs/agent.jsonl` | `$XDG_STATE_HOME` | `~/.local/state/<name>/logs/agent.jsonl` | `log_jsonl` |
| `skills/` | `$XDG_STATE_HOME` | `~/.local/state/<name>/skills/` | `skills_dir` |
| `scheduler_state.json` | `$XDG_STATE_HOME` | `~/.local/state/<name>/scheduler_state.json` | `scheduler_state` |
| `scheduler_commands.json` | `$XDG_STATE_HOME` | `~/.local/state/<name>/scheduler_commands.json` | `scheduler_commands` |
| `scheduler_jobs.json` | `$XDG_STATE_HOME` | `~/.local/state/<name>/scheduler_jobs.json` | `scheduler_jobs` |
| `job_execution_log.jsonl` | `$XDG_STATE_HOME` | `~/.local/state/<name>/job_execution_log.jsonl` | `job_execution_log` |
| `results_memory.json` | `$XDG_DATA_HOME` | `~/.local/share/<name>/results_memory.json` | *(no field; `data_home`-joined)* |
| `longterm_memory.json` | `$XDG_DATA_HOME` | `~/.local/share/<name>/longterm_memory.json` | *(no field; `data_home`-joined; legacy/backfill-only)* |
| `graph_memory_backfill_state.json` | `$XDG_DATA_HOME` | `~/.local/share/<name>/graph_memory_backfill_state.json` | *(no field; `data_home`-joined)* |
| `downloads/` | workspace | `<workspace_dir>/downloads/` | *(derived from config)* |

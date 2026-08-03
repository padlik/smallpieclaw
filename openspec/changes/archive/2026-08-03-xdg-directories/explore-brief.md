# Explore Brief: xdg-directories

## Alternatives Rejected

| Alternative | Rejected Because |
|---|---|
| Keep relative `data/` dir in agent_home | Blocks clean upgrades — git pull / venv wipe destroys data |
| Single XDG bucket (everything under `~/.local/state`) | Semantically wrong; mixes config, data, cache, state |
| Option B (layered config lookup: cwd fallback) | More complexity for no benefit; clean break is preferred |
| Bundled/default skills shipped with code | Skills are user-created only; separates code lifecycle from user config lifecycle |
| Keep `_AGENT_DIR` as reference point | Ties path resolution to code directory; must be retired |
| Auto-migrate silently with no manual option | User wants `migrate.py` runnable standalone + auto on first `--agent-name` run |

## Full XDG Mapping (all paths, no "e.g.")

| File / Dir | XDG Base | Resolved Path | Notes |
|---|---|---|---|
| `config.toml` | `$XDG_CONFIG_HOME` | `~/.config/<agent_name>/config.toml` | was `_AGENT_DIR/config.toml` |
| `scheduler.toml` | `$XDG_CONFIG_HOME` | `~/.config/<agent_name>/scheduler.toml` | was `_AGENT_DIR/scheduler.toml` |
| `memory.json` | `$XDG_DATA_HOME` | `~/.local/share/<agent_name>/memory.json` | was `data/memory.json` |
| `graph_memory/` | `$XDG_DATA_HOME` | `~/.local/share/<agent_name>/graph_memory` | was `data/graph_memory`; WAL files alongside |
| `tool_index.json` | `$XDG_CACHE_HOME` | `~/.cache/<agent_name>/tool_index.json` | regeneratable; never migrated |
| `scheduler_state.json` | `$XDG_STATE_HOME` | `~/.local/state/<agent_name>/scheduler_state.json` | was `data/scheduler_state.json` |
| `scheduler_commands.json` | `$XDG_STATE_HOME` | `~/.local/state/<agent_name>/scheduler_commands.json` | was `data/scheduler_commands.json` |
| `scheduler_jobs.json` | `$XDG_STATE_HOME` | `~/.local/state/<agent_name>/scheduler_jobs.json` | was `data/scheduler_jobs.json` |
| `job_execution_log.jsonl` | `$XDG_STATE_HOME` | `~/.local/state/<agent_name>/job_execution_log.jsonl` | was `data/job_execution_log.jsonl` |
| `agent.pid` | `$XDG_RUNTIME_DIR` | `/run/user/<uid>/<agent_name>/agent.pid` | fallback: `~/.local/state/<agent_name>/agent.pid` if `$XDG_RUNTIME_DIR` unset |
| `secrets.toml` | `$XDG_STATE_HOME` | `~/.local/state/<agent_name>/secrets.toml` | already here; no migration needed |
| `logs/agent.log` | `$XDG_STATE_HOME` | `~/.local/state/<agent_name>/logs/agent.log` | already here; no migration needed |
| `logs/agent.jsonl` | `$XDG_STATE_HOME` | `~/.local/state/<agent_name>/logs/agent.jsonl` | already here; no migration needed |
| `skills/` | `$XDG_STATE_HOME` | `~/.local/state/<agent_name>/skills/` | was `_AGENT_DIR/skills/`; user-created only |
| `downloads/` | workspace | `<workspace_dir>/downloads/` | default: `~/Documents/downloads/` |

## Config Parameters Removed

All of the following are removed from `[paths]` in `config.toml` and derived automatically:
`data_dir`, `tool_index_file`, `memory_file`, `pid_file`, `downloads_dir`, `log_file`, `file_vault`, `graph_memory.db_path`, `skills_dir`

## Key Cross-Module Data Flows

### Startup (main.py)
```
main.py --agent-name <name>
  → xdg.py: resolve all paths from agent_name + XDG env vars
  → create all XDG dirs (idempotent)
  → check for old layout → auto-migrate if needed (migrate.py logic)
  → if ~/.config/<name>/config.toml missing → error with clear message
  → warn if config contains relative paths
  → pass resolved paths to _run() / all constructors
```

### Path resolution (new xdg.py module)
```
xdg_paths(agent_name: str) → XDGPaths dataclass
  config_home  = $XDG_CONFIG_HOME or ~/.config
  data_home    = $XDG_DATA_HOME or ~/.local/share
  state_home   = $XDG_STATE_HOME or ~/.local/state
  cache_home   = $XDG_CACHE_HOME or ~/.cache
  runtime_dir  = $XDG_RUNTIME_DIR or None → fallback to state_home
  → returns all concrete paths as XDGPaths fields
```

### Config bootstrap (main.py)
```
_AGENT_DIR retired entirely
sys.argv / argparse: --agent-name <name> (required)
config_file = xdg_paths(name).config_home / name / "config.toml"
```

### Migration (migrate.py)
```
migrate.py --agent-name <name> [--source <agent_home_dir>]
  → detect old layout: agent_home has config.toml / data/ / skills/
  → copy config.toml → XDG config
  → copy scheduler.toml → XDG config
  → copy data/memory.json → XDG data
  → copy data/graph_memory* → XDG data
  → copy data/scheduler_*.json, job_execution_log.jsonl → XDG state
  → copy skills/ → XDG state/skills/
  → delete data/tool_index.json (not migrated; regenerates)
  → write migration sentinel: XDG state / migrated_from_<timestamp>.sentinel
  → auto-triggered by main.py on first --agent-name run if old layout detected
```

### Tests (conftest.py)
```
tmp_xdg fixture (replaces tmp_agent_dir):
  monkeypatch all XDG_* env vars → tmp_path subdirs
  agent writes to /tmp/pytest-xxx/... not real home
```

### nsjail (nsjail_config.py)
```
skills_dir whitelist: ~/.local/state/<agent_name>/skills/  (read-only)
replaces project-relative skills/ path
```

### Systemd user service
```
ExecStart = /path/to/venv/bin/python main.py --agent-name <name>
WorkingDirectory = agent_home (for venv; no longer affects data paths)
XDG_RUNTIME_DIR set automatically by systemd-logind for user services
```

## Known Open Questions (all resolved)

| Question | Resolution |
|---|---|
| Bootstrap: how to find config before knowing agent_name? | `--agent-name` CLI arg (Option A) |
| Skills: bundled with code or user-created only? | User-created only (Option A) |
| Strict XDG split or pragmatic single bucket? | Strict XDG per-bucket |
| Config location: state or config? | `$XDG_CONFIG_HOME` (correct per XDG) |
| scheduler.toml in scope? | Yes, moves to `$XDG_CONFIG_HOME` |
| Relative paths in config.toml? | Document as unsupported; warn at startup |
| Migration: auto or manual? | Both: `migrate.py` standalone + auto on first run |
| First run with missing config? | Error + create all XDG dirs |
| agent_home after change? | Pure code; freely replaceable via git pull + venv recreate |

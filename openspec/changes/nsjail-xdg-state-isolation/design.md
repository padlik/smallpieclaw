# Design: nsjail XDG State Isolation

## Overview

Move nsjail-specific mutable state out of the agent installation directory and into an XDG state directory that is never mounted by the sandbox. Add the agent installation directory to the trusted-mount blocklist.

## File Locations (Before → After)

| File | Before | After |
|---|---|---|
| `trusted_dirs.json` | `{data_dir}/trusted_dirs.json` | `{nsjail_state_dir}/trusted_dirs.json` |
| nsjail config tempfiles | `/tmp/<random>` (unchanged) | `/tmp/<random>` (unchanged) |

`nsjail_state_dir` defaults to `$XDG_STATE_HOME/<agent_name>/nsjail` (or `~/.local/state/<agent_name>/nsjail` when `XDG_STATE_HOME` is unset). This directory is created on first use with `os.makedirs(..., exist_ok=True)`.

## Constructor / Parameter Chain

```
main.py
  _AGENT_DIR = dirname(abspath(__file__))
  nsjail_state_dir = os.path.join(xdg_state_home, agent_name, "nsjail")
      ↓
BuiltinExecutor.__init__(
    ...,
    nsjail_state_dir: str = "",   # new
    agent_dir: str = "",          # new
)
      ↓
NsjailConfigBuilder.__init__(
    ...,
    nsjail_state_dir: str = "",   # replaces data_dir for trusted_dirs.json path
    agent_dir: str = "",          # new — added to mount blocklist
)
```

`data_dir` remains on `NsjailConfigBuilder` for any future non-trusted-dirs state, but `trusted_dirs.json` is no longer read from it.

## Trusted Mount Blocklist (Extended)

`_load_trusted_mounts` builds a per-call reject set from:

1. `_BLOCKED_SYSTEM_PREFIXES` — existing tuple (`/etc`, `/proc`, `/sys`, `/dev`, `/run`, `/boot`, `/root`)
2. `_agent_dir` — real path of the agent installation dir (new)
3. XDG dirs derived at construction time:
   - `os.path.realpath(nsjail_state_dir)` and its parent (`<xdg_state_home>/<agent_name>`)
   - `os.path.expanduser("~/.local/share")` (XDG data home, typical path)

Any entry whose resolved real path equals or starts with a blocked prefix is rejected with a `WARNING` log and skipped.

## Config Field

`AgentConfig.nsjail_state_dir: str = ""` — empty string means "compute from XDG default at runtime." Operators can override to an absolute path for container or chroot deployments where `~/.local/state` is not available.

The actual resolution happens in `main.py`:

```python
xdg_state_home = os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
nsjail_state_dir = app_cfg.agent.nsjail_state_dir or os.path.join(
    xdg_state_home, app_cfg.agent.name, "nsjail"
)
```

## Trusted Dir Commands (`/dir add`, `/dir remove`)

The built-in commands that read and write `trusted_dirs.json` (`builtin_tools/files.py`) must use the same `nsjail_state_dir` path. `BuiltinExecutor` holds `_nsjail_state_dir` and passes it to the file tool handler. This replaces the current `data_dir`-relative path.

## No Migration

`data/trusted_dirs.json` is left in place but no longer read. Operators who upgrade lose previously configured trusted dirs and must re-add them with `/dir add`. This is acceptable: the old path was insecure and the list is typically short (1-3 entries). A migration script is out of scope.

## Key Decisions

| Decision | Rationale |
|---|---|
| XDG state (not data) | State = mutable runtime data. XDG_STATE_HOME is the correct bucket per XDG spec. |
| Compute default in `main.py`, not `NsjailConfigBuilder` | Builder should not access the process environment; env resolution belongs at the composition root. |
| Block XDG dirs in mount validator | Defense-in-depth: even if a future bug allows writing to XDG state, the validator prevents mounting it. |
| No migration | Old path was the vulnerability. Preserving it (even as read-only fallback) keeps the attack surface open during the transition window. |

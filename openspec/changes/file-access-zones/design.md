## Overview

A `TrustedZoneChecker` component wraps all `file_*` built-in tool operations. Every path request is classified into one of four zones; the zone determines whether the operation proceeds silently or triggers a confirmation prompt. Path resolution uses `os.path.realpath()` throughout (resolves symlinks and normalises `..`) to prevent zone bypass via symlink or traversal.

## Component: TrustedZoneChecker

**Module:** `builtin_tools/access_control.py`

### Zone classification (priority order)

```
1. INTERNAL      → auto-allow (agent-owned dirs, no gate)
2. TRUSTED       → auto-allow (default + user-added dirs, no gate)
3. REQUEST_GRANT → auto-allow (per-request dir grant, cleared each request)
4. UNRECOGNISED  → confirmation prompt with extended options
```

### Internal dirs (auto-bypass, built from PathsConfig at construction)

| Field | Default |
|-------|---------|
| `paths.tools_dir` | `tools/` |
| `paths.tools_generated_dir` | `tools_generated/` |
| `paths.data_dir` | `data/` |
| `paths.skills_dir` | `skills/` |
| `paths.prompts_dir` | `prompts/` |
| `log_path(cfg)` | `~/.local/state/<agent>/logs/` |
| `vault_path(cfg)` | `~/.local/share/<agent>/secrets.toml` |

All resolved via `os.path.realpath()` at construction time.

### Default trusted dirs (protected, non-removable)

| Field | Default |
|-------|---------|
| `paths.workspace_dir` *(new)* | `~/Documents` |
| `paths.downloads_dir` | `<agent_home>/downloads` |
| `paths.tmp_dir` | `/tmp/<agent_name>` |

### User-added trusted dirs

Persisted in `data/trusted_dirs.json` (written atomically via `_atomic_save_json()`):

```json
[
  {"path": "/abs/resolved/path", "added": "2026-07-17T14:30:00"}
]
```

Loaded at startup. Updated in-place when user taps **[Add to trusted]**.

### Request grants

In-memory `set[str]` of absolute directory paths. Populated by **[Allow this request]** — stores `os.path.dirname(realpath(path))` of the requested file. Cleared by `reset_request_grants()` at `react_loop()` entry (once per user message cycle).

### Public API

```python
class TrustedZoneChecker:
    def classify(self, path: str) -> ZoneClassification: ...
    def grant_for_request(self, path: str) -> None: ...
    def reset_request_grants(self) -> None: ...
    def add_trusted(self, path: str) -> None: ...  # persists
    def remove_trusted(self, index: int) -> str: ...  # returns removed path
    def list_user_trusted(self) -> list[TrustedDir]: ...

class ZoneClassification(Enum):
    INTERNAL = "internal"
    TRUSTED = "trusted"
    REQUEST_GRANT = "request_grant"
    UNRECOGNISED = "unrecognised"

# Derived action: INTERNAL / TRUSTED / REQUEST_GRANT → allow; UNRECOGNISED → confirm

@dataclass
class TrustedDir:
    path: str
    added: str  # ISO8601
```

## Changes to file_* tools (`builtin_tools/files.py`)

Current confirmation triggers replaced by zone check:

| Tool | Before | After |
|------|--------|-------|
| `file_write` | always confirms | confirms only if `classify(path) == UNRECOGNISED` |
| `file_patch` | always confirms | confirms only if `classify(path) == UNRECOGNISED` |
| `file_read` | confirms if sensitive pattern | confirms if `classify(path) == UNRECOGNISED` |
| `file_diff` | no confirmation | zone-checked before each read; if either path is UNRECOGNISED, stage confirmation |
| `file_send` | no confirmation | zone-checked before read; if UNRECOGNISED, stage confirmation |

The existing `_is_sensitive_path()` check stacks on top of zone classification — a path inside a trusted zone that matches a sensitive pattern (`.key`, `.env`, `secrets.*`, etc.) still triggers confirmation.

The existing `_is_sensitive_path()` gate stacks on top of zone classification for ALL `file_*` tools including `file_diff` and `file_send`. A sensitive-pattern match on either path triggers confirmation regardless of zone membership, preventing silent reads of `.env`, `.key`, or `secrets.*` files even through the comparison and send paths.

## Confirmation UX: Extended options

Out-of-zone prompts gain two new inline buttons alongside the existing `[Approve]` / `[Deny]`:

- **[Allow this request]** — calls `checker.grant_for_request(path)`; operation proceeds; future accesses to the same directory within this request are silent.
- **[Add to trusted]** — calls `checker.add_trusted(os.path.dirname(realpath(path)))`; persists to `data/trusted_dirs.json`; operation proceeds; future accesses silent permanently.

Both new buttons are handled in `telegram_callbacks.py`.

The `[Allow this request]` and `[Add to trusted]` buttons are shown ONLY when the zone is `UNRECOGNISED`. When confirmation is triggered by the sensitive-pattern gate on a TRUSTED path, only `[Approve]` and `[Deny]` are shown.

## /dir command (`telegram_commands.py`)

```
/dir list    → list user-added trusted dirs only (defaults not shown)
/dir del N   → remove entry N, no confirmation required
```

Output format for `/dir list`:
```
Trusted directories:
  1. /Users/paul/projects/myapp
  2. /srv/shared/data
```

Empty state: `No custom trusted directories added yet.`
Invalid index: `No trusted directory #N.`

Default trusted dirs are intentionally excluded from the listing — they are fixed by config and cannot be removed.

## ReactContext changes

`ReactContext` dataclass (`react_loop.py`) gains:

```python
trusted_zone_checker: TrustedZoneChecker
```

`react_loop()` calls `trusted_zone_checker.reset_request_grants()` once at loop entry, before tool dispatch begins.

All `file_*` handlers receive `TrustedZoneChecker` via the existing `owner` / context pattern (same as `_owner.max_output`, `_owner._requires_confirmation()`).

## Concurrency and sub-agent scoping

Each agent instance (main agent and each sub-agent) receives its OWN `TrustedZoneChecker`
instance. They share the same persisted `data/trusted_dirs.json` (loaded once at construction),
but the in-memory request grant set is NOT shared. This prevents concurrent sub-agents from
clearing each other's grants via `reset_request_grants()`.

Sub-agents are headless callers. The existing `_headless_confirm_bridge` remains active for
UNRECOGNISED zone prompts from sub-agents — the operator is notified out of band and the
sub-agent blocks for approval. Writes inside TRUSTED zones by sub-agents proceed silently
(same as the main agent), which is the intended relaxation of prior always-confirm behavior.

## Trust store protection

`data/trusted_dirs.json` is inside `data_dir` (INTERNAL zone), but is explicitly excluded
from the INTERNAL auto-allow rule. `file_write` and `file_patch` targeting `trusted_dirs.json`
are treated as UNRECOGNISED and require confirmation. The `TrustedZoneChecker.add_trusted()`
method writes directly via `_atomic_save_json()` (bypasses `file_write`) and is only reachable
through the confirmed `[Add to trusted]` button — it is not an LLM-accessible path.

## main.py wiring

ONE `TrustedZoneChecker` instance is constructed and injected into both `BuiltinExecutor` and `ReactContext`:

```python
trusted_zone_checker = TrustedZoneChecker(
    paths_config=app_cfg.paths,
    data_dir=str(paths.data_dir),
    agent_name=app_cfg.agent.agent_name,
)
builtin_executor.trusted_zone_checker = trusted_zone_checker  # files.py reads via self._owner.trusted_zone_checker.classify()
react_context = ReactContext(
    ...,
    trusted_zone_checker=trusted_zone_checker,  # used for reset_request_grants()
)
```

## config_schema.py change

`PathsConfig` gains one new field:

```python
workspace_dir: str = "~/Documents"
```

Resolved via `os.path.expanduser()` at `TrustedZoneChecker` construction (then `realpath()`).

## New data file

`data/trusted_dirs.json` — created on first **[Add to trusted]** action, absent until then. The `TrustedZoneChecker` handles missing file gracefully (starts with empty user-added list).

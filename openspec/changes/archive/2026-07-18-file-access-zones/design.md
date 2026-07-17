## Overview

A `TrustedZoneChecker` component wraps all `file_*` built-in tool operations. Every path request is classified into one of three zones; the zone determines whether the operation proceeds silently or triggers a confirmation prompt. Path resolution uses `os.path.realpath()` throughout (resolves symlinks and normalises `..`) to prevent zone bypass via symlink or traversal.

Agent-internal directories (data, tools, skills, prompts, XDG dirs) are intentionally outside the trusted set. The LLM accesses internal data through dedicated built-in tools (`memory_read`, `secret_get`, `log_query`, etc.); direct `file_*` access to internal paths requires user confirmation like any other unrecognised path.

## Component: TrustedZoneChecker

**Module:** `builtin_tools/access_control.py`

### Zone classification (priority order)

```
1. TRUSTED       → auto-allow (default + user-added dirs; subject to mode)
2. REQUEST_GRANT → auto-allow (per-request dir grant, cleared each request)
3. UNRECOGNISED  → confirmation prompt with extended options
```

### Default trusted dirs (protected, non-removable)

| Field | Default |
|-------|---------|
| `paths.workspace_dir` *(new)* | `~/Documents` |
| `paths.downloads_dir` | `<agent_home>/downloads` |
| `paths.tmp_dir` | `/tmp/<agent_name>` |

All resolved via `os.path.expanduser()` then `os.path.realpath()` at construction time.

### User-added trusted dirs

Persisted in `data/trusted_dirs.json` (written atomically via `_atomic_save_json()`):

```json
[
  {"path": "/abs/resolved/path", "added": "2026-07-17T14:30:00", "mode": "rw"}
]
```

The `mode` field controls write access:
- `"rw"` (default, and default when field absent): auto-allow reads and writes
- `"r"`: auto-allow reads; confirm writes

Loaded at startup. Updated in-place when user taps **[Add to trusted]** (always `"rw"` when added from button). Default trusted dirs are always `"rw"`.

### Request grants

In-memory `set[str]` of absolute directory paths. Populated by **[Allow this request]** — stores `os.path.dirname(realpath(path))` of the requested file. Cleared by `GrantTracker.reset()` at `react_loop()` entry (once per user message cycle).

### Public API

```python
class TrustedZoneChecker:
    def classify(self, path: str, operation: str = "write", request_grants: frozenset[str] = frozenset()) -> ZoneClassification: ...
    # operation: "read" | "write" — defaults to "write" (fail-safe: omitting operation never silently allows writes to r-mode dirs)
    def add_trusted(self, path: str, mode: str = "rw") -> None: ...   # persists to data/trusted_dirs.json
    def remove_trusted(self, index: int) -> str: ...    # returns removed path
    def list_user_trusted(self) -> list[TrustedDir]: ...

class GrantTracker:
    """Per-executor ephemeral request grant set. One instance per BuiltinExecutor."""
    def add(self, path: str) -> None: ...       # grants parent dir of path
    def reset(self) -> None: ...                # called at react_loop() entry
    def snapshot(self) -> frozenset[str]: ...   # thread-safe snapshot for classify()

class ZoneClassification(Enum):
    TRUSTED = "trusted"
    REQUEST_GRANT = "request_grant"
    UNRECOGNISED = "unrecognised"

# Derived action: TRUSTED / REQUEST_GRANT → allow; UNRECOGNISED → confirm

@dataclass
class TrustedDir:
    path: str
    added: str   # ISO8601
    mode: str = "rw"  # "r" | "rw"
```

## Changes to file_* tools (`builtin_tools/files.py`)

Current confirmation triggers replaced by zone check:

| Tool | Before | After |
|------|--------|-------|
| `file_write` | always confirms | auto-allow if TRUSTED(rw)/REQUEST_GRANT; confirm otherwise |
| `file_patch` | always confirms | auto-allow if TRUSTED(rw)/REQUEST_GRANT; confirm otherwise |
| `file_read` | confirms if sensitive pattern | auto-allow if TRUSTED/REQUEST_GRANT; confirm if UNRECOGNISED |
| `file_diff` | no confirmation | zone-checked before each read (`operation="read"`); if either path is UNRECOGNISED, stage confirmation |
| `file_send` | no confirmation | zone-checked before read (`operation="read"`); if UNRECOGNISED, stage confirmation |

Each tool passes `operation="read"` or `operation="write"` to `classify()`. The existing `_is_sensitive_path()` check stacks on top of zone classification for ALL `file_*` tools — a sensitive-pattern match triggers confirmation regardless of zone membership, preventing silent reads of `.env`, `.key`, or `secrets.*` files even inside trusted zones, through comparison paths, and through send paths.

## Confirmation UX: Extended options

Out-of-zone prompts gain two new inline buttons alongside the existing `[Approve]` / `[Deny]`:

- **[Allow this request]** — calls `grant_tracker.add(path)`; operation proceeds; future accesses to the same directory within this request are silent.
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
  1. /Users/paul/projects/myapp  [rw]
  2. /srv/archive  [r]
```

Empty state: `No custom trusted directories added yet.`
Invalid index: `No trusted directory #N.`

Default trusted dirs are intentionally excluded from the listing — they are fixed by config and cannot be removed.

## ReactContext changes

`ReactContext` dataclass (`react_loop.py`) gains two fields:

```python
trusted_zone_checker: TrustedZoneChecker
grant_tracker: Optional[GrantTracker]   # per-executor GrantTracker for request-grant isolation
```

`react_loop()` calls `ctx.grant_tracker.reset()` once at loop entry, before tool dispatch begins. This clears all in-request directory grants from the previous cycle.

`file_*` handlers call `classify(path, operation, request_grants=ctx.grant_tracker.snapshot())` to pass the current grant set without holding a lock across I/O. The `GrantTracker` lives on `BuiltinExecutor` (one per executor); `ReactContext` holds a reference to the same object for lifecycle management (`reset()`).

All `file_*` handlers receive `TrustedZoneChecker` via the existing `owner` / context pattern (same as `_owner.max_output`, `_owner._requires_confirmation()`).

## Concurrency and sub-agent scoping

Each `BuiltinExecutor` instance owns its own `GrantTracker`. The persistent trust store
(`_user_trusted`, `_default_trusted_dirs`) and the `TrustedZoneChecker` instance are shared
between the main agent and sub-agents. The `GrantTracker` is per-executor, but since
sub-agents currently reuse the main agent's executor, grant sets are effectively shared in
practice. Full per-sub-agent `GrantTracker` isolation is tracked as a follow-up improvement.
`GrantTracker.reset()` is called at each `react_loop()` entry; a sub-agent loop-entry reset
clears the parent's in-flight grants (fail-safe behavior).

Sub-agents are headless callers. The existing `_headless_confirm_bridge` remains active for
UNRECOGNISED zone prompts from sub-agents — the operator is notified out of band and the
sub-agent blocks for approval. Writes inside TRUSTED zones by sub-agents proceed silently
(same as the main agent), which is the intended relaxation of prior always-confirm behavior.

## main.py wiring

ONE `TrustedZoneChecker` instance is constructed and injected into both `BuiltinExecutor` and `ReactContext`:

```python
trusted_zone_checker = TrustedZoneChecker(
    paths_config=app_cfg.paths,
    data_dir=str(paths.data_dir),
    agent_name=app_cfg.agent.agent_name,
    vault_path=vault_path(cfg),
)
builtin_executor.trusted_zone_checker = trusted_zone_checker  # files.py reads via self._owner.trusted_zone_checker.classify()
# grant_tracker is owned by BuiltinExecutor; ReactContext holds a reference for lifecycle (reset at loop entry)
react_context = ReactContext(
    ...,
    trusted_zone_checker=trusted_zone_checker,
    grant_tracker=builtin_executor.grant_tracker,
)
```

## config_schema.py change

`PathsConfig` gains one new field:

```python
workspace_dir: str = "~/Documents"
```

Resolved via `os.path.expanduser()` at `TrustedZoneChecker` construction (then `realpath()`).

## New data file

`data/trusted_dirs.json` — created on first **[Add to trusted]** action, absent until then. The `TrustedZoneChecker` handles missing file gracefully (starts with empty user-added list). Entries without a `mode` field are treated as `"rw"` for backward compatibility.

**Defense-in-depth overrides:** `data/trusted_dirs.json` and the vault file (resolved from `vault_path(cfg)`, honoring `$SPC_VAULT_FILE`) are always classified as UNRECOGNISED by `classify()`, even if a parent directory (e.g. `data/` or `~/.local/share/<agent>/`) appears in the trusted set. This prevents a user-added trusted dir from accidentally making the trust store itself silently writable or the vault silently readable. The vault path must be injected into `TrustedZoneChecker` at construction time (via `vault_path=vault_path(cfg)`) so the override follows the resolved env-configurable location rather than any hardcoded default.

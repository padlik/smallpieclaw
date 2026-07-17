## Why

The agent currently has no zone-based file access control — any OS-accessible path can be read or written, with only pattern-based confirmation gates for a narrow set of sensitive filenames. This allows the agent to silently touch arbitrary locations on the filesystem that the user never intended, and requires confirmation even for routine writes to the user's own workspace.

## What Changes

- **New `workspace_dir` config field** in `PathsConfig` (default: `~/Documents`) — the primary user workspace, always trusted.
- **New `TrustedZoneChecker`** component (`builtin_tools/access_control.py`) that classifies every `file_*` path as: trusted (user zones, auto-allow), request-granted (per-request directory grant, allow), or unrecognised (prompt user). Agent-internal directories (agent home, XDG dirs) are unrecognised — the LLM must use dedicated built-in tools (`memory_read`, `secret_get`, `log_query`) for agent-internal data access.
- **Trusted directory entries support an optional `mode` field** (`"r"` or `"rw"`, default `"rw"`) — a read-only trusted dir auto-allows reads but requires confirmation for writes.
- **`file_*` tool confirmation logic replaced** — reads/writes inside trusted (`rw`) zones no longer require confirmation; reads/writes outside trusted zones now prompt regardless of filename patterns.
- **New confirmation options** added to out-of-zone prompts: `[Approve]`, `[Deny]`, `[Allow this request]` (grants parent directory for the current request), `[Add to trusted]` (persists to `data/trusted_dirs.json`).
- **New `/dir` Telegram command** to list and remove user-added trusted directories.
- **Per-request grant reset** in `react_loop.py` — request-scoped grants are cleared at the start of each new user message cycle.

## Capabilities

### New Capabilities

- `file-access-zones`: Zone-based classification and access control for all `file_*` tool operations, with trusted zone definitions (default + user-added with optional read-only mode) and per-request directory grants.
- `trusted-dir-management`: Telegram `/dir` command and `[Add to trusted]` inline button to manage user-defined trusted directories; persisted in `data/trusted_dirs.json`.

### Modified Capabilities

_(none — no existing spec-level behaviour is changing; this adds a new access layer)_

## Impact

- `builtin_tools/access_control.py` — new module (`TrustedZoneChecker`, `ZoneClassification`, `GrantTracker`, `TrustedDir`)
- `builtin_tools/files.py` — all `file_*` tools call zone check with operation type; confirmation logic revised
- `config_schema.py` — `PathsConfig` gains `workspace_dir` field
- `react_loop.py` — `GrantTracker.reset()` called at loop entry
- `main.py` — construct `TrustedZoneChecker`, inject into `ReactContext`
- `telegram_commands.py` — `/dir list` and `/dir del <n>` handlers
- `telegram_callbacks.py` — `[Allow this request]` and `[Add to trusted]` inline button handlers
- `data/trusted_dirs.json` — new runtime file (user-added trusted dirs with optional mode)

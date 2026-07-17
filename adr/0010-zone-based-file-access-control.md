# Use zone-based access control for file operations

## Status

Accepted

## Date

2026-07-17

## Supersedes

None

## Context

Prior to this change, `file_*` built-in tools had no path-based access boundary. `file_write` and `file_patch` confirmed unconditionally; `file_read` confirmed only for a small set of hard-coded sensitive filename patterns. The agent could silently read or write any OS-accessible path outside those patterns. This exposed the user's broader filesystem to unintended access and caused unnecessary confirmation friction for routine writes to the user's own workspace.

A replacement model is needed that clearly separates the user's working areas (trusted zones) from the rest of the filesystem, and enforces that boundary at the `file_*` tool layer before any operation is performed.

Agent-internal directories (data, tools, skills, prompts, XDG dirs) are deliberately excluded from the trusted set. `file_*` access to internal paths is confirmation-gated, not silently accessible. Dedicated built-in tools (`memory_read`, `secret_get`, `log_query`) are the intended access path for agent-internal data; direct `file_*` access requires explicit user confirmation.

## Decision

Use `TrustedZoneChecker` as the single canonical access-control gate for all `file_*` built-in tool operations. Every path is resolved via `os.path.realpath()` and classified into one of three zones in priority order:

1. **TRUSTED** — user workspace directories (default: `~/Documents`, `downloads/`, `/tmp/<agent>`), plus any user-added directories: auto-allow without confirmation, subject to per-entry read-only mode.
2. **REQUEST_GRANT** — a directory granted by the user for the current request via `[Allow this request]`: auto-allow until the request boundary; cleared at the start of each new user message cycle.
3. **UNRECOGNISED** — everything else (including all agent-internal directories): stage the operation and present a confirmation prompt with four options: `[Approve]`, `[Deny]`, `[Allow this request]`, `[Add to trusted]`.

Trusted directory entries support an optional `mode` field (`"r"` or `"rw"`, default `"rw"`). A `"r"`-mode entry auto-allows reads but returns UNRECOGNISED for writes, enabling read-only access to shared or archive directories without exposing them to modification.

The existing sensitive-pattern gate (`_is_sensitive_path()`) stacks on top of zone classification and is never bypassed by trusted-zone membership.

## Consequences

- Good, because routine writes to the user's workspace proceed without confirmation friction.
- Good, because accidental agent access to arbitrary filesystem locations is gated at the tool layer before any effect.
- Good, because `file_*` access to agent-internal paths (memory, vault, logs) is confirmation-gated; the LLM is directed to dedicated built-ins for internal data access rather than raw file paths.
- Good, because the trusted set is user-controlled and dynamically extensible without restarting the agent.
- Good, because `realpath()` prevents zone bypass via symlink or `..` traversal.
- Good, because trusted dirs can be marked read-only to allow inspection without write risk.
- Bad, because all future `file_*` tools must integrate with `TrustedZoneChecker` — omitting the check is a silent security regression.
- Bad, because the behavior change for `file_write`/`file_patch` (no longer always confirming inside trusted zones) is a UX inversion that may surprise operators upgrading from prior versions.
- Neutral, because the shell tool is explicitly out of scope for this boundary and will be addressed in a future sandboxing change.
- Bad, because the default `workspace_dir = ~/Documents` encompasses the agent's own source tree on typical developer machines (e.g. `~/Documents/develop/<repo>`), making the agent capable of silently modifying its own code without confirmation under the default configuration.
- Neutral, because ONE `TrustedZoneChecker` instance is shared between the main agent and sub-agents (the persistent trust store is read-only after construction); request-grant isolation is provided by a per-`BuiltinExecutor` `GrantTracker`, not by separate checker instances.

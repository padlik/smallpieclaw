# Proposal — unify-access-control

## Why

The files-and-folders access plane scatters one decision — "may the agent touch this path?" — across 7+ lists in 5 files (three nsjail blocklist tuples, zone tiers, sensitive-path patterns, an approve-all allowlist, ephemeral request grants), with a dynamic trust store (`trusted_dirs.json`) that can silently diverge from what the jail actually mounts, and an approval asymmetry where the main agent can bulk-approve `shell` while sub-agents cannot (verified C-16 finding). This change replaces that sprawl with a single three-tier path policy and one grant mechanism, closes the agent's XDG state home entirely, and adds a dedicated run-results exchange directory.

## What Changes

- **Three-tier path policy** replaces all per-tool and per-jail blocklists:
  - **Tier 0 — Prohibited**: hardcoded dict (system dirs, credential homes `~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.kube`, `~/.docker`), derived at startup (agent XDG data+state home, `~/.config/<agent>`, vault file), plus config-appended `prohibited_dirs`. Absolute priority; access is a **hard tool error with a reason string** — never a prompt, never grantable, immune to operator recklessness (e.g. `allowed_dirs = ["/"]` changes nothing for Tier 0). Inode/hardlink-alias defense retained for prohibited files.
  - **Tier 1 — Agent-controlled** (hardcoded, operator cannot modify): `workspace_dir`, `downloads_dir`, `/tmp/<agent>` (rw, as today); `~/.<agent>/skills` (r; **moved out of the XDG data home**); `~/.<agent>/results` (rw; **new**).
  - **Tier 2 — Operator allowed**: static config `allowed_dirs`, extend-only, rw-only, cannot remove Tier-1 defaults. Allowed-dirs containing prohibited paths: prohibited wins (startup warning; no jail mount for that entry — no partial bind mounts, the jail never silently becomes a no-op).
- **BREAKING** — dynamic trust removed: `TrustedZoneChecker`, `trusted_dirs.json`, "Add to trusted"/"Allow this request" zone buttons, `GrantTracker`, and the `_is_sensitive_path` file-pattern overlay are **deleted**. **BREAKING** — the main-agent cross-tool bulk-approve mechanism is also deleted: `auto_approve_tools` and `_ALLOWED_APPROVE_ALL_TOOLS` (including the "Approve all" button/config on the main-agent path) are removed; the unified grant ledger is the only standing-grant mechanism. No migration; stale `trusted_dirs.json` is silently ignored. Credential *directories* are now hard-denied (Tier 0) instead of prompted.
- **Unified grant ledger**: one grant type `(tool, dir-recursive)`, two lifetimes — **prompt** (until end of the current user-message cycle) and **session** (until `/reset`). Confirmation UX: three buttons (Confirm / Till /reset / Deny). Grants are in-memory only, never persisted.   **Sink-side policy vetoes** (from the C-16 analysis): `shell` and `secret_get` can never hold standing grants — enforced in the grant-ledger check, not the Telegram boundary. Their prompts render **Confirm/Deny only** (no "Till /reset" affordance — no dead buttons). Prohibited paths are immune to grants. Sub-agent prompt-lifetime grants expire with the sub-agent's run (single depth-0-owned ledger, no upward privilege leak).
- **Agent state home fully closed**: XDG data/state/config dirs are Tier 0; the read-only session-logs mount inside the jail is removed (logs are served by the existing `log_query` SQLite store). Skills migrate one-time (copy on first run) to `~/.<agent>/skills`.
- **Run-results exchange directory**: `~/.<agent>/results/<trace-id>/` for oversized-payload transfer (sub-agent handoffs, oversized shell output artifacts relocated from `_finalize_shell_log`; vault-secret redaction retained). Mounted rw in the jail; whole dir per session (config is session-static). **Keep until manual clean** — no automatic retention.
- **BREAKING** — nsjail mount table is now derived **once per session** from the static tier configuration (per-call trusted-dirs re-read and per-call config builds removed). Per-call command/env injection (`-E` flags) unchanged. Linux-only; macOS not supported at this stage.
- PathPolicy (frozen, session-static) is constructed at startup with conflict validation; "checker unwired" degradation is replaced by a **startup error** — fail closed everywhere.

## Capabilities

### New Capabilities
- `path-policy`: three-tier path classification (prohibited / agent-controlled / operator-allowed), startup derivation and conflict validation, hard-error semantics, inode-alias defense, session-static frozen policy object.
- `approval-grants`: the grant ledger — `(tool, dir-recursive)` grant type, prompt/session lifetimes, sub-agent run scoping, sink-side tool vetoes (`shell`, `secret_get`), prohibition immunity, in-memory-only storage, three-button confirmation UX, clearing at prompt-cycle end and `/reset`.
- `agent-results-dir`: `~/.<agent>/results/<trace-id>/` directory contract — run-scoped subdirectories, rw jail mount, oversized-output artifact relocation, manual-clean retention.

### Modified Capabilities
- `file-access-zones`: file tools (`file_read`, `file_diff`, `file_write`, `file_patch`, `file_send`) gate through `path-policy` + `approval-grants` instead of `TrustedZoneChecker`/request grants/sensitive-path overlay; prohibited paths return hard errors; confirm-before-read preserved (`file_patch` content-oracle fix).
- `trusted-dir-management`: removed — the dynamic trust store, add/remove/list/reload flows, and zone buttons are deleted; superseded by static `allowed_dirs` config consumed by `path-policy`.
- `nsjail-shell-sandboxing`: mount table derived from tiers (Tier 1 + Tier 2 minus prohibited overlap), built once per session; session-logs mount removed; blocklist tuples collapse into Tier 0.
- `skills-dir-sandbox-mount`: skills relocate to `~/.<agent>/skills`, mounted read-only from the new location; one-time first-run copy from the XDG data home.
- `agent-scoped-directories`: `~/.<agent>/skills` and `~/.<agent>/results` join the agent-scoped directory set; the XDG data/state/config homes become prohibited.

## Impact

- **Code**: `builtin_tools/access_control.py` (mostly deleted → replaced by `path_policy.py`), `builtin_tools/files.py` (gating rewire), `builtin_tools/patterns.py` (file patterns removed), `nsjail_config.py` (mount-table rebuild + once-per-session build), `builtin_executor.py` (grant ledger, `PendingConfirmations` lifecycle), `confirmation.py` (grant ledger + lifetimes), `telegram_interface.py` / `telegram_callbacks.py` (three-button UX, zone-button removal), `agent_controller.py` (sub-agent grant scoping, depth-0 run-end clearing), `react_loop.py` (prompt-cycle grant clearing at loop entry), `config_schema.py` (`prohibited_dirs`, `allowed_dirs`), `main.py` (startup derivation + conflict validation), `trace_context.py` (results subdirs, reuse only).
- **BREAKING behavior**: previously trusted dirs prompt again until re-added via config; sensitive-name prompts inside trusted zones are gone (weaker? no — credential dirs hard-denied); `config.toml.example`/README updated for the new config sections.
- **Tests**: zone-checker, zone-button, request-grant, sensitive-path, and approve-all suites rewritten around `path-policy` and `approval-grants`; `test_subagent_approve_all.py` crafted-callback defense retained at the new sink.
- **Dependencies**: none new (stdlib only). Relies on the completed SQLite log store (`log_query`) for closing the XDG home.
- **Out of scope**: execution plane (dangerous-shell patterns, confirm modes, PTY/subprocess internals, seccomp), macOS support.
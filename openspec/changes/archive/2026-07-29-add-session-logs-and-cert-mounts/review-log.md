# Review Log — add-session-logs-and-cert-mounts

## proposal Round 1 — 2026-07-29
### 🔴 Fixed
- `shell-env-management` was incorrectly listed as a Modified Capability — the shell log path change belongs in the new `session-log-management` capability. Removed `shell-env-management` from Modified Capabilities; folded the path-change description into `session-log-management`.
### 🟡 Addressed
- `/reset discard` lifecycle not distinguished from `/reset` (save) — added explicit "save and discard variants" language.
- Sub-agents not acknowledged — added "Sub-agents inherit the main conversation's conversation_id and share its session_logs folder."
- CA cert mount `mandatory: false` not mentioned — added "non-mandatory" and "mandatory: false so the jail still starts if the path is absent."
### 🔴 Outstanding
- (none)

## proposal Round 2 — 2026-07-29
### 🔴 Fixed
- (none — all Round 1 fixes verified correct)
### 🟡 Addressed
- (none)
### 🔴 Outstanding
- (none)
### Verdict: PASS — proposal frozen.

## design + specs Round 1 — 2026-07-29
### 🔴 Fixed
- (none — no critical issues)
### 🟡 Addressed
- `conversation-persistence` spec had implementation detail (`context_io._save_context`) in requirement text — replaced with behavioral guarantee ("the save MUST be atomic").
- `nsjail-shell-sandboxing` spec had code-level reference (`_BLOCKED_SYSTEM_PREFIXES`) in requirement text — replaced with behavioral guarantee ("system mount, not subject to trusted-directory blocklist").
- `session-log-management` spec retention cleanup didn't handle missing directory on first run — added "If the session_logs/ directory does not exist (first startup), the cleanup is a no-op — no error is logged."
### 🔴 Outstanding
- (none)
### Verdict: PASS — design and specs frozen.

## tasks Round 1 — 2026-07-29
### 🔴 Fixed
- Task 1.3 did not mention corrupted conversation file handling — added explicit note that `_load_context` handles corrupted JSON gracefully (logs warning, returns fresh ShortTermMemory).
- Task 1.5 did not specify atomic write for conversation_id file on /reset rotation — added "atomically (temp file + os.replace)".
### 🟡 Addressed
- Task 1.2 atomic write pattern underspecified — added "(temp file + os.replace)".
- Task 2.4 missing config.toml.example entries for session_logs_retention_days and allow_net CA cert behavior — expanded task to include both.
### 🔴 Outstanding
- (none)
### Verdict: PASS — tasks frozen.
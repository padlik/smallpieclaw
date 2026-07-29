# Mount session_logs read-only inside the nsjail sandbox

## Status

Accepted, supersedes the archived decision in `openspec/changes/archive/2026-07-28-add-shell-isolation-improvements/design.md` Decision 3 ("shell_logs is excluded from sandbox mounts")

## Date

2026-07-29

## Supersedes

Archived change `2026-07-28-add-shell-isolation-improvements` — specifically Decision 3: "`shell_logs` is excluded from sandbox mounts" which stated that `shell_logs` is an internal overflow directory, not used for inter-script exchange, and should not be mounted inside the sandbox. This ADR reverses that decision for the renamed `session_logs` directory, but with a read-only mount instead of read-write.

## Context

The archived `add-shell-isolation-improvements` change established that `shell_logs/` (now renamed `session_logs/`) should not be mounted inside the nsjail sandbox. The rationale was that it is an internal overflow directory for large command outputs, not an inter-script exchange surface, and exposing it inside the jail was unnecessary.

This decision is being revisited because of a new requirement: sandboxed shell commands should be able to read prior large command outputs directly, without the agent having to pipe them back through `file_read → shell`. This enables a workflow where a script inside the jail can inspect a prior output artifact by path.

The key safety constraint is that the mount is **read-only** (`rw: false`). A sandboxed script can read prior outputs but cannot write to, spam, or fill disk in `session_logs`. The agent process (outside the jail) retains exclusive write access. This is a narrower reversal than a read-write mount would be.

ADR-0015 ("nsjail configuration state must reside outside the sandbox's write scope") is not violated: `session_logs` is agent-written output, not sandbox configuration, and the mount is read-only so the sandbox cannot modify it. The principle — the sandbox cannot write to agent-controlled state — is respected.

## Decision

1. The active conversation's `session_logs` folder (`~/.local/state/<agent>/session_logs/<conversation_id>/`) MUST be bind-mounted read-only inside the nsjail jail at the same host path (`src == dst`, `is_bind: true`, `rw: false`, `mandatory: false`).
2. The mount is a system mount (like `/dev/null`, `/dev/zero`, `/usr`), not a trusted-directory mount — it is not subject to the trusted-directory blocklist and does not require operator approval.
3. The same-host-path mount (`src == dst`) ensures the LLM sees one absolute path that works with both `file_read` (agent-side) and `cat` (shell-side). No jail-internal vs host-internal path mapping is needed.
4. If the `session_logs` directory does not exist or the `session_logs_dir` kwarg is empty, the mount is skipped (graceful degradation, `mandatory: false`).

## Consequences

- **Positive**: Sandboxed shell commands can read prior large command outputs directly by path, enabling richer inter-call workflows without agent-mediated piping.
- **Positive**: The read-only constraint means a sandboxed script cannot write to, spam, or fill disk in `session_logs` — the attack surface is read-only.
- **Positive**: The same-host-path mount eliminates the cognitive load of mapping jail-internal paths to host paths — one path, both tools.
- **Negative**: Reverses a prior archived decision. The reversal is scoped: read-only, not read-write. A sandboxed script can read but not write.
- **Neutral**: ADR-0015 is not violated — `session_logs` is output, not configuration, and the mount is read-only.
- **Neutral**: The `mandatory: false` flag means the jail still starts if the directory is absent (e.g., first startup before any shell call).
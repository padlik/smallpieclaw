# Use nsjail for shell command isolation with configurable confirmation

## Status

Accepted, supersedes ADR-0011 (partial — shell auto-approval invariant only)

## Date

2026-07-22

## Supersedes

ADR-0011 — specifically the invariant on line 31: "`shell` is never auto-approved — it remains always-confirmed for the main agent and always-blocked for sub-agents, regardless of any approve-all grant." This ADR amends the main-agent half of that invariant. The sub-agent fail-closed half is preserved unchanged.

## Context

ADR-0011 established that `shell` is never auto-approved for the main agent — every dangerous shell command requires operator confirmation regardless of any approve-all grant. This was correct when the shell tool ran commands with no kernel-level isolation: the only barriers were a regex denylist and operator confirmation.

This change introduces nsjail as a third shell backend (`shell_backend: "nsjail"`). When nsjail is active, shell commands run inside a Linux sandbox with mount/PID/net/user/IPC/UTS/cgroup namespace isolation, seccomp-bpf syscall filtering, and cgroup v2 resource limits. The jail confines the blast radius to the project directory and explicitly trusted RW directories — the host filesystem, network, and system resources are protected by the kernel.

With kernel-level isolation in place, the operator may reasonably choose to relax the per-command confirmation gate. A configurable `shell_nsjail_confirm_mode` field (`"always"` | `"adaptive"` | `"never"`) lets the operator decide how much to trust the sandbox:
- `"always"` (default): all dangerous patterns confirm — backward-compatible with ADR-0011.
- `"adaptive"`: only `resource` patterns (fork bombs, bounded by cgroup `pids_max`) skip confirmation.
- `"never"`: all patterns skip confirmation when nsjail is active.

The `"never"` mode carries a documented risk: RW-mounted host directories (project, trusted RW dirs) are not protected by the jail — `rm -rf` can delete real host files under those mounts. The operator accepts this risk by explicitly setting `"never"`.

When nsjail is NOT active (fallback to subprocess), the confirm mode always behaves as `"always"` — the sandbox is not present, so ADR-0011's original invariant is preserved.

The sub-agent fail-closed half of ADR-0011 is unchanged: sub-agents (caller_depth >= 1) always fail closed for shell commands, regardless of confirm mode.

## Decision

1. The shell tool's confirmation gate becomes configurable via `shell_nsjail_confirm_mode` when the nsjail backend is active. The operator chooses the trust level.
2. The default is `"always"` — backward-compatible with ADR-0011.
3. When nsjail is not active, the gate always behaves as `"always"` — ADR-0011's invariant is preserved.
4. Sub-agents (depth >= 1) fail closed for shell regardless of confirm mode — the sub-agent half of ADR-0011 is preserved.
5. The `"never"` mode's data-loss risk (RW-mounted host dirs are not protected by the jail) is documented in the design and surfaced to the operator.

## Consequences

- Good, because operators who trust the nsjail sandbox can reduce confirmation fatigue and let the agent work autonomously.
- Good, because the default (`"always"`) preserves the existing behavior — no forced change for existing deployments.
- Good, because the sub-agent fail-closed rule is preserved — sub-agents never run dangerous shell commands unattended.
- Bad, because `"never"` mode allows destructive commands (`rm -rf`, `chmod 777`) to run without confirmation, and these can delete real host files under RW-mounted directories. Mitigated by: opt-in, default is `"always"`, system prompt warns the LLM when `"never"` is active.
- Neutral, because this amends only the main-agent half of ADR-0011. The per-prompt TTL, shared approval set, and sub-agent fail-closed rules are all preserved.
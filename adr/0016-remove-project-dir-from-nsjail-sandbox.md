# Remove project_dir mount from nsjail sandbox

## Status

Accepted, supersedes ADR-0012 (partial — project_dir mount scope only)

## Date

2026-07-28

## Supersedes

ADR-0012 — specifically the context on line 19: "The jail confines the blast radius to the project directory and explicitly trusted RW directories" and the implicit assumption that the project directory is a user codebase. This ADR removes the project directory from the sandbox's writable scope. The `shell_nsjail_confirm_mode` decision in ADR-0012 remains unchanged.

## Context

ADR-0012 established nsjail as the shell isolation backend with a `project_dir` mounted read-write as the sandbox working directory. ADR-0015 established that sandbox configuration state (`trusted_dirs.json`) must live outside the sandbox's write scope to prevent mount-list injection attacks.

The `project_dir` was wired to `_AGENT_DIR` — the directory containing `main.py` — which means the entire agent installation (source code, memory store, config, scheduler state, tool index) is mounted read-write inside every sandboxed shell command. This is a larger writable surface than `trusted_dirs.json` ever was, and it exposes all agent internals to any sandboxed command.

The `project_dir` concept was designed for a scenario where the agent works on a user's codebase. In practice, it was wired to the agent's own code directory, creating a security hole that contradicts the isolation principle behind ADR-0012 and ADR-0015.

Additionally, the blanket `/home` entry in `_BLOCKED_SYSTEM_PREFIXES` prevents legitimate user paths from being mounted as trusted dirs or skills dirs in systemd-user deployments where everything naturally lives under `/home`. The targeted blocks (`~/.ssh`, `~/.local`, `~/.config`) already cover sensitive subdirs.

## Decision

1. The nsjail sandbox MUST NOT mount any project directory by default. The `project_dir` parameter is removed from `NsjailConfigBuilder` and `BuiltinExecutor`.
2. The sandbox `cwd` is set to `/tmp` (the session tmpdir, already mounted RW).
3. The sandbox's writable scope is confined to `/tmp` (session scratch) and explicitly-approved trusted RW directories only.
4. `/home` is removed from `_BLOCKED_SYSTEM_PREFIXES`. Targeted sensitive-subdir blocks (`~/.ssh`, `~/.local`, `~/.config`, `~/.gnupg`) remain in `_blocked_user_prefixes`.
5. The vault file default location moves from `~/.local/share/<agent>/secrets.toml` (XDG_DATA_HOME) to `~/.local/state/<agent>/secrets.toml` (XDG_STATE_HOME), consolidating all agent state under one XDG directory. A one-time migration copies the old vault if the new path doesn't exist.

## Consequences

- **Positive**: Agent source code, memory, config, and scheduler state are no longer writable from inside the sandbox. A compromised or buggy shell command cannot corrupt agent internals.
- **Positive**: Legitimate user paths under `/home` (skills, workspace, trusted dirs) can now be mounted in the sandbox, fixing the systemd-user deployment scenario.
- **Positive**: All agent state files are consolidated under `~/.local/state/<agent>/` — one directory to back up, inspect, and manage.
- **Positive**: ADR-0012's `"never"` confirm mode becomes less dangerous — `rm -rf` can only damage `/tmp` and explicitly-trusted RW dirs, not the agent's code.
- **Negative**: Shell commands that previously read/wrote files under `_AGENT_DIR` without a trusted-dir entry will fail. Operators must add needed paths via `/dir add`. This is the intended security improvement.
- **Negative**: Shell commands that relied on `cwd` being the agent directory must use absolute paths or `cd` to a trusted dir.
- **Neutral**: The `shell_nsjail_confirm_mode` decision (ADR-0012) is unaffected — the confirmation gate operates on command patterns, not on mount scope.
# Explore Brief: nsjail XDG State Isolation

## Problem

`trusted_dirs.json` lives at `{data_dir}/trusted_dirs.json`, where `data_dir` is within the agent installation directory. The agent installation directory is bind-mounted read-write into the nsjail sandbox as the project working directory. This means:

1. An LLM-driven shell command running inside the sandbox can write arbitrary content to `data/trusted_dirs.json`.
2. On the next `builder.build()` call, `_load_trusted_mounts` reads the (now-attacker-controlled) file and generates nsjail mount entries from it.
3. The attacker can inject a path like `/etc` or `/root/.ssh` as a trusted mount, gaining read (or read-write) access to host files outside the intended project scope.

This is a complete sandbox escape vector via a persistent, LLM-writable config file.

## Secondary Risk: Agent Dir as Trusted Mount

The `_load_trusted_mounts` validator blocks `/etc`, `/proc`, `/sys`, etc. via `_BLOCKED_SYSTEM_PREFIXES`. However, the agent installation directory itself is not in the blocklist. A crafted `trusted_dirs.json` entry pointing to the agent dir (or a subdirectory like `data/`) would re-mount the already-mounted project dir with different permissions, or expose agent source code and secrets to the sandboxed process.

## Shell Logs / Nsjail State

Any nsjail-related state files (future shell execution logs, nsjail config caches, etc.) should similarly live outside the sandbox's write scope. Currently only `trusted_dirs.json` is the concern; XDG placement covers the general case.

## What Must Change

| Current | Problem | Fix |
|---|---|---|
| `{data_dir}/trusted_dirs.json` | In sandbox write scope | Move to `~/.local/state/<agent_name>/nsjail/` |
| Agent dir not in mount blocklist | Can be injected as trusted mount | Add agent dir + XDG dirs to `_load_trusted_mounts` blocklist |
| `NsjailConfigBuilder` takes only `data_dir` | No way to know agent dir at build time | Add `agent_dir` constructor param |

## Constraints

- `data_dir` is already used for other agent state (tool index, memory). Only the nsjail-specific subdirectory moves.
- The XDG state dir must be created if it doesn't exist (on first build).
- Config key `nsjail_state_dir` should be user-overridable for non-standard deployments.
- The nsjail config tempfiles are already written to `/tmp` (not in agent dir) — no change needed there.

## Chosen Approach

Move `trusted_dirs.json` (and any future nsjail state) to `~/.local/state/<agent_name>/nsjail/`. Pass `agent_dir` to `NsjailConfigBuilder` at construction time. Block agent dir + XDG state/data dirs from the trusted mount validator.

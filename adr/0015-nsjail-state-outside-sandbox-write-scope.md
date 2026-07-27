# nsjail sandbox configuration state must reside outside the sandbox's write scope

## Status

Accepted

## Date

2026-07-24

## Supersedes

None

## Context

ADR-0012 established nsjail as the shell isolation backend. That decision introduced `NsjailConfigBuilder`, which reads `trusted_dirs.json` to generate bind-mount entries. The file was initially stored at `{data_dir}/trusted_dirs.json` — a path inside the agent installation directory, which is bind-mounted read-write into the sandbox as the project working directory.

This created a complete sandbox escape: an LLM-driven shell command running inside the jail can overwrite `trusted_dirs.json`, injecting arbitrary host paths as trusted mounts. On the next `builder.build()` call those paths are mounted and the jail is compromised. The attack requires no special privilege — any `echo ... > data/trusted_dirs.json` from inside the sandbox suffices.

## Decision

Any file or state that influences nsjail sandbox configuration — mount lists, policy files, cgroup parameters — **must reside outside the sandbox's write scope**.

Concretely:
- `trusted_dirs.json` (and any future nsjail state) is stored under `$XDG_STATE_HOME/<agent_name>/nsjail/`, which is never bind-mounted into the sandbox.
- The agent installation directory (`_AGENT_DIR`) is added to the trusted-mount blocklist in `_load_trusted_mounts`, preventing any `trusted_dirs.json` entry from re-exposing it.
- XDG state and data dirs for the agent are similarly blocked from being added as trusted mounts.
- The path used for nsjail state is resolved at the composition root (`main.py`) from `XDG_STATE_HOME` or an operator-provided override; `NsjailConfigBuilder` receives the resolved path and does not access the environment itself.

This principle applies to all future nsjail-adjacent features: if a file can influence what gets mounted or what syscalls are permitted, it must be stored where the sandbox cannot write it.

## Consequences

- **Positive**: The sandbox can no longer modify its own mount configuration. Trusted-dir injection attacks are structurally impossible.
- **Positive**: Agent installation dir is protected from re-mount via trusted-dirs injection.
- **Negative**: Existing `data/trusted_dirs.json` entries are not migrated. Users who upgrade must re-add trusted directories with `/dir add`.
- **Neutral**: Operators who override `XDG_STATE_HOME` (e.g., container deployments) must also set `nsjail_state_dir` in agent config; the default XDG computation cannot be relied upon in non-standard layouts.

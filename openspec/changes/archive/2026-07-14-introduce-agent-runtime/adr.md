# ADR Review Manifest

- Status: completed
- Review date: 2026-07-13

## Review Summary

ADR review completed for this change. The change introduces one durable architectural decision: use `AgentRuntime` as the construction boundary for agent executions while preserving separate supervision, visibility, and orchestration ownership.

## In-Force ADRs Reviewed

- `adr/0003-use-toml-vault-format.md` — in force; not affected by this change.
- `adr/0004-structured-primary-agent-logging.md` — in force; compatible with this change's requirement to preserve explicit trace/log behavior.
- `adr/0005-use-sub-agent-supervisor-boundary.md` — in force; this change keeps supervision lifecycle outside `AgentRuntime`.
- `adr/0006-use-source-categories-for-agent-visibility.md` — in force; this change keeps runtime profiles separate from source visibility/capacity categories.

## New Durable ADRs Created

- `adr/0007-use-agent-runtime-for-construction.md` — records the runtime construction-boundary decision introduced by this change.

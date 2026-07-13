# ADR Review Manifest

- Status: completed
- Review date: 2026-07-12

## Review Summary

ADR review completed for this change. The change introduces one durable architectural decision: use a `SubAgentSupervisor` boundary for background sub-agent supervision while keeping `spawn_agent` as the model-facing tool shim.

## In-Force ADRs Reviewed

- `adr/0003-use-toml-vault-format.md` — in force; not affected by this change.
- `adr/0004-structured-primary-agent-logging.md` — in force; compatible with this change's requirement to preserve explicit trace/log behavior across background threads.

## New Durable ADRs Created

- `adr/0005-use-sub-agent-supervisor-boundary.md` — records the durable supervision-boundary decision introduced by this change.

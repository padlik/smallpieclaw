# ADR Review Manifest

- Status: completed
- Review date: 2026-07-13

## Review Summary

ADR review completed for this change. The change introduces one durable architectural decision: use explicit sub-agent source categories for running-agent visibility and global capacity counting.

## In-Force ADRs Reviewed

- `adr/0003-use-toml-vault-format.md` — in force; not affected by this change.
- `adr/0004-structured-primary-agent-logging.md` — in force; compatible with this change's requirement to preserve explicit trace/log behavior.
- `adr/0005-use-sub-agent-supervisor-boundary.md` — in force; this change resolves the running-agent visibility and cap semantics intentionally deferred by ADR-0005.

## New Durable ADRs Created

- `adr/0006-use-source-categories-for-agent-visibility.md` — records the source-category visibility and capacity decision introduced by this change.

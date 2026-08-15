# ADR Review Manifest

- Status: completed
- Review date: 2026-07-24

## Review Summary

ADR review completed for this change.

## In-Force ADRs Reviewed

- ADR-0001: Use file-backed provider secrets
- ADR-0002: Vault secret manager
- ADR-0003: Use TOML vault format
- ADR-0004: Structured primary agent logging
- ADR-0005: Use sub-agent supervisor boundary
- ADR-0006: Use source categories for agent visibility
- ADR-0007: Use agent runtime for construction
- ADR-0008: Use facade handler package for builtin tools
- ADR-0009: Native tool calling
- ADR-0010: Zone-based file access control
- ADR-0011: Per-prompt approval scope
- ADR-0012: Use nsjail for shell isolation
- ADR-0013: Use ULID for globally-unique prompt IDs

No in-force ADRs require supersession. ADR-0013 (ULID prompt IDs) is the most directly relevant — this change builds on it by using the ULID as the lookup key for `show()` and the archive snapshot's `prompt_id` field.

## New Durable ADRs Created

- `adr/0014-use-dual-write-archive-for-prompt-registry.md` — Establishes the dual-write persistence pattern (event log + snapshot archive) with in-memory eviction at 100 records and lock-free archive scanning for search. This is a long-term architectural commitment that will constrain future changes to the prompt registry's storage and concurrency model.
# ADR Review Manifest

- Status: completed
- Review date: 2026-09-25

## Review Summary

ADR review completed for this change. All 32 top-level repository ADR files (ADR-0001 through ADR-0032) were inspected; supersession links were followed to identify the in-force context. The durable fail-closed shell decision is recorded in [`adr/0033-fail-closed-model-issued-shell.md`](../../../adr/0033-fail-closed-model-issued-shell.md).

## In-Force ADRs Reviewed

- ADR-0003 — Use TOML for agent-scoped vault files
- ADR-0004 — Use structlog for structured-primary agent logging (backbone remains in force; storage mechanism partially superseded by ADR-0025)
- ADR-0005 — Use SubAgentSupervisor as the sub-agent supervision boundary
- ADR-0006 — Use source categories for running agent visibility and capacity
- ADR-0007 — Use AgentRuntime for agent execution construction (partially superseded by ADR-0020)
- ADR-0008 — Use a façade + handler-module package for built-in tools
- ADR-0009 — Use native tool calling as primary path with text-based JSON fallback
- ADR-0012 — Use nsjail for shell command isolation (isolation and active-jail confirmation decisions remain; fallback/default assumptions partially superseded by ADR-0033)
- ADR-0013 — Use ULID for globally-unique prompt IDs
- ADR-0014 — Use dual-write archive for prompt registry search and bounded memory
- ADR-0015 — nsjail sandbox configuration state outside sandbox write scope
- ADR-0016 — Remove project_dir mount from nsjail sandbox (partially superseded by ADR-0018)
- ADR-0019 — Use XDG Base Directory Specification for all agent storage paths
- ADR-0020 — Remove fallback_models and add per-model context window
- ADR-0021 — Use disk-persisted checkpoints for LLM error recovery
- ADR-0022 — Use push-model context monitor for context-window tracking
- ADR-0023 — Record exactly-once lifecycle logging with component log isolation
- ADR-0025 — Use SQLite WAL store for structured logs
- ADR-0026 — Unify file access into PathPolicy
- ADR-0027 — Single grant ledger for standing consent
- ADR-0028 — Agent dot-home and closed XDG state
- ADR-0029 — Preserve OAuth refresh tokens across silent refreshes
- ADR-0030 — Non-interactive long-lived MCP OAuth sessions
- ADR-0031 — Restore OAuth provider state from persisted tokens
- ADR-0032 — Use list-detail split for log introspection

## Fully Superseded Records

- ADR-0001 → ADR-0002 (file-backed secrets)
- ADR-0002 → ADR-0003 (agent-scoped vault format)
- ADR-0010 → ADR-0026 (zone-based file access)
- ADR-0011 → ADR-0024 and ADR-0027 (approval/grant decisions; ADR-0012 separately superseded its shell auto-approval invariant in part)
- ADR-0017 → ADR-0028 (read-only session-log mount)
- ADR-0018 → ADR-0026 (default-trusted nsjail mounts)
- ADR-0024 → ADR-0027 (confirmation coordinator/signaling decision; reviewed as relevant historical context, not an in-force constraint)

## New Durable ADRs Created

- [`adr/0033-fail-closed-model-issued-shell.md`](../../../adr/0033-fail-closed-model-issued-shell.md) — partially supersedes ADR-0012 for default/activation and unconfined fallback only; leaves its valid isolation and active-jail confirmation decisions in force.

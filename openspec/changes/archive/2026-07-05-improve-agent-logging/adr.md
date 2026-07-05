# ADR Review Manifest

- Status: completed
- Review date: 2026-07-05

## Review Summary

ADR review completed for this change. The repository ADR set was read and the supersession graph was walked (`ADR-0001 → ADR-0002 → ADR-0003`). Only **ADR-0003** is currently in force; ADR-0001 and ADR-0002 are superseded historical records. The design was checked for coherence with ADR-0003 and for durable decisions not already captured by an in-force ADR.

## In-Force ADRs Reviewed

- **ADR-0003 — Use TOML for agent-scoped vault files** (Accepted, supersedes ADR-0002). Relevant here only because the secret-redaction processor sources its known values from the agent-scoped vault this ADR governs; the logging change is coherent with it and does not revisit it.

Superseded (historical context only, not in force): ADR-0001 (file-backed provider secrets), ADR-0002 (agent-scoped vault).

## New Durable ADRs Created

- **ADR-0004 — Use structlog for structured-primary agent logging** (`adr/0004-structured-primary-agent-logging.md`). Records the durable commitment to adopt `structlog` (integrated with stdlib via `ProcessorFormatter`) as the logging backbone: a structured-primary dual-sink architecture with `structlog.contextvars` identity, a closed `event_type` taxonomy, and a redaction processor, plus the deferral of a SQLite store. Does not supersede any prior ADR.

Not recorded as a separate ADR: the XDG state-directory log location. This applies the existing agent-scoped-directories path convention (state paths derive from `agent_name`, independent of `agent_home`) to logs and is captured in the `agent-scoped-directories` spec delta rather than as a new architectural commitment.

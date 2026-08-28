# ADR Review Manifest

- Status: completed
- Review date: 2026-08-27

## Review Summary

ADR review completed for this change. All 22 existing repository-level ADRs under `adr/` were checked for the supersession graph — none is superseded, so all are in force. ADR-0004 (structured-primary agent logging) is the backbone this change builds on; it is NOT superseded (see below). Design decisions D1–D5 were evaluated against the durable-decision bar (long-term commitment, affects future changes, not already captured or intentionally diverging): D1 (dedup pairs), D5 (precedent note), and the XDG path naming are tactical implementation details and do not qualify. D2 (single TOOL_FAILED / ERROR reserved) and D3 (component log isolation) together form one durable logging contract and are captured in a single new ADR.

## In-Force ADRs Reviewed

- ADR-0004 (structured-primary agent logging) — backbone; companion relationship, explicitly not superseded
- ADR-0019 (XDG base directory layout) — governs the `graph_memory.log` path placement; respected
- ADR-0008 (facade handler package), ADR-0009 (native tool calling), ADR-0020, ADR-0022 — checked for logging/observability interactions; no conflicts
- Remaining in-force ADRs (0001–0003, 0005–0007, 0010–0018, 0021) — graph built; not touched by this change

## New Durable ADRs Created

- `adr/0023-record-exactly-once-lifecycle-logging.md` — record-exactly-once lifecycle logging + component log isolation (companion to ADR-0004; `LogEvent` stays closed; background components route to dedicated component logs; `ERROR` reserved as zero-emitter member)
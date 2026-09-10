# ADR Review Manifest

- Status: completed
- Review date: 2026-09-11

## Review Summary

ADR review completed for this change. All 28 repository-level ADRs under `adr/` were read and the supersession graph built from their `Supersedes` fields: ADR-0001 is superseded by ADR-0002, which is superseded by ADR-0003; ADR-0003 through ADR-0028 are in force (26 records).

The change's design decisions (D1-D8) were each tested against the durability bar — long-term architectural commitment, affects future changes beyond this one, not already captured by an in-force ADR:

- D1 (job-state ownership stays in `Scheduler`, `exec_schedule` remains a stateless leaf) — application of the in-force ADR-0008 facade/leaf pattern, not a new commitment.
- D3 (`run_now` manual override executes through the standard supervised scheduled-run path with `SOURCE_SCHEDULED`) — application of the in-force ADR-0005 supervisor boundary and ADR-0007 runtime construction; no boundary change.
- D2/D4-D8 (partial-patch semantics, `show_job` accessor, allowlist depth guard, tool-layer validation, error contracts, schema/descriptor/brief updates) — feature-level behavior of one tool, fully captured by the frozen delta specs; not architectural.

No in-force ADR is contradicted, revisited, or requires supersession.

## In-Force ADRs Reviewed

- ADR-0003 through ADR-0028 (in force); most relevant to this change:
  - ADR-0005 (sub-agent supervisor boundary) — `run_now` manual override and scheduled runs continue to flow through `SubAgentSupervisor` with scheduler-internal controls in per-submission options.
  - ADR-0007 (agent runtime construction) — scheduled-run sub-agents continue to be constructed via the `SCHEDULED_AGENT` runtime profile.
  - ADR-0008 (facade handler package for builtin tools) — new actions are new dispatch branches in the leaf `exec_schedule`; all state, locking, validation, and persistence stay in `Scheduler`.
  - ADR-0006 (source categories for agent visibility) — manual triggers remain source `scheduled`; no new visibility category.
- ADR-0001 and ADR-0002 are historical (superseded by the 0002→0003 chain) and treated as context only.

## New Durable ADRs Created

- None - no major durable architectural decisions were introduced.
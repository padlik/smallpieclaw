# ADR Review Manifest

- Status: completed
- Review date: 2026-09-19

## Review Summary

ADR review completed for this change.

One major durable architectural decision was identified meeting all three
bars (long-term commitment, affects future changes, not captured by any
in-force ADR): the two-tier list-detail log introspection contract. It is now
recorded as a repository-level ADR. The design.md decisions (D1–D7) do not
warrant *separate* ADRs — the architectural essence of the D-items that rises
above implementation detail (the whole-set `total_matched` walk semantics, the
65536-char hard cap, the oversized-dump escape hatch and redaction
preservation) is folded into ADR-0032's contract; the remainder is tactical
implementation detail owned by the frozen design/specs artifacts.

## In-Force ADRs Reviewed

- ADR-0004 — structured-primary agent logging (backbone, redaction layer, taxonomy; in force)
- ADR-0008 — facade handler package for builtin tools (in force)
- ADR-0025 — SQLite WAL store for structured logs (in force; its query-mechanism boundary explicitly covers this change's filter-surface extension — no divergence)
- ADR-0026 — unified file access into PathPolicy (in force; dump destination is Tier 1)
- ADR-0028 — agent dot-home and closed XDG state (in force; `~/.<agent>/results/` is the artifact exchange surface; supersedes ADR-0017 for session-logs concerns)
- ADR-0029/0030/0031 — OAuth/MCP provider state (in force, reviewed for completeness; not touched by this change)

## New Durable ADRs Created

- `adr/0032-use-list-detail-split-for-log-introspection.md` — Use list-detail
  split for log introspection: two-tier budget-aware read contract over the
  unchanged store (id-anchored `before_id` walk on the list tier; single-id
  full-fidelity `log_detail` with 65536-char cap and oversized-dump escape
  hatch on the detail tier). Status: Accepted, supersedes none — extends the
  ADR-0025 read-path contract without revising it.
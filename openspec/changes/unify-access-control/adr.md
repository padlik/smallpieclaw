# ADR Review Manifest

- Status: completed
- Review date: 2026-09-03

## Review Summary

ADR review completed for this change. Three durable architectural decisions were
identified in design.md; each is recorded as a new repository-level ADR. The full
supersession graph (25 existing ADRs) was walked before drafting; characterizations
were spot-verified against the prior ADR files.

## In-Force ADRs Reviewed

- ADR-0008 — facade/handler package for built-in tools (deferred ConfirmationCoordinator
  item at :52-54 is resolved by this change via subsumption, not merge — see ADR-0027)
- ADR-0010 — zone-based file access control (superseded by ADR-0026)
- ADR-0011 — per-prompt approval scope (superseded by ADR-0027; shell-never-approved
  invariant preserved and strengthened via sink veto)
- ADR-0012 — nsjail for shell isolation (untouched; execution plane out of scope)
- ADR-0015 — nsjail state outside sandbox write scope (unaffected; state home now simply
  never mounted)
- ADR-0017 — session-logs RO mount in nsjail (superseded/reversed by ADR-0028)
- ADR-0018 — default trusted zones mounted into nsjail (superseded by ADR-0026)
- ADR-0019 — XDG base directory layout (remains in force for path resolution; access
  policy to those paths changes per ADR-0028)
- ADR-0024 — unified confirmation signaling in coordinator (superseded by ADR-0027;
  signaling flow preserved, grant storage replaced)
- ADR-0025 — SQLite WAL log store (enabler: log_query makes closing the XDG home lossless)

## New Durable ADRs Created

- `adr/0026-unify-file-access-into-path-policy.md` — single frozen three-tier PathPolicy
  (prohibited / agent-controlled / operator-allowed) replacing the scattered blocklists,
  zone checker, and sensitive-path overlay; jail mount table derived from the same tiers,
  built once per session. Supersedes ADR-0010, ADR-0018.
- `adr/0027-single-grant-ledger-for-standing-consent.md` — one grant type
  `(tool, dir-recursive)` × lifetime {prompt, session}, depth-0-owned ledger, sink-side
  vetoes for shell/secret_get, prohibition immunity, sub-agent run scoping, in-memory
  only. Supersedes ADR-0011, ADR-0024.
- `adr/0028-agent-dot-home-and-closed-xdg-state.md` — agent dot-home
  (`~/.<agent>/skills` r, `~/.<agent>/results` rw) for operator-facing exchange surfaces;
  XDG data/state/config homes become Tier 0 prohibited; session-logs jail mount removed
  (logs via log_query SQL). Supersedes ADR-0017.
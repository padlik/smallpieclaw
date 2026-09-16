# ADR Review Manifest

- Status: completed
- Review date: 2026-09-16

## Review Summary

ADR review completed for this change. The design (design.md D1–D5) was scanned for durable architectural commitments. One qualifies and was recorded as a new repository-level ADR: ADR-0031 captures the storage restore contract (a provider reconstructed from persisted tokens restores its absolute expiry and the authorization server's token endpoint from the persisted state) — sibling to ADR-0029's retention contract. D1's `issued_at`-based math, D4's storage mechanics, and D5's test strategy are tactical implementation details that do not meet the ADR bar.

## In-Force ADRs Reviewed

All 30 ADRs under `adr/` were reviewed via the supersession graph (none carry active supersession links; all are in force). None are contradicted by this change; this change extends two of them:

- `adr/0029-preserve-oauth-refresh-tokens-across-silent-refreshes.md` — extended, not contradicted: the retention contract stands; this change makes the refresh branch it guards actually reachable for reconstructed providers.
- `adr/0030-noninteractive-long-lived-mcp-oauth-sessions.md` — the D3 handoff that reconstructs providers from disk is the trigger for the defect being fixed; the non-interactive degradation semantics are untouched.
- `adr/0019-xdg-base-directory-layout-for-agent-storage.md` — token-file location, permissions, and atomic write unchanged; the schema gains one optional top-level key.
- Others (0001–0018, 0020–0028) — reviewed; no interaction with OAuth provider construction or token persistence.

## New Durable ADRs Created

- `adr/0031-restore-oauth-provider-state-from-persisted-tokens.md` — providers reconstructed from persisted tokens SHALL restore absolute expiry (`issued_at + expires_in`) and the persisted `oauth_metadata` block (token endpoint) on initialization; storage persists the discovered metadata after a completed exchange and carries it forward across writes (design D1/D2/D3/D4).
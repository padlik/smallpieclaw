# ADR Review Manifest

- Status: completed
- Review date: 2026-09-14

## Review Summary

ADR review completed for this change. The design (design.md D1–D5) was scanned for durable architectural commitments. Two qualify and were recorded as new repository-level ADRs: ADR-0029 captures D1/D2 (refresh-token preservation contract), and ADR-0030 captures D3 plus D4's durable background-degradation principle (needs_auth degradation with recovery hint). Only D4's specific error-string/indicator mechanics and D5's registration-map hygiene are tactical implementation details that do not meet the ADR bar.

## In-Force ADRs Reviewed

All 28 ADRs under `adr/` were reviewed via the supersession graph (none carry active supersession links; all are in force). None are contradicted by this change. Most relevant:

- `adr/0019-xdg-base-directory-layout-for-agent-storage.md` — token-file location/format untouched (D1 keeps the same path, permissions, atomic write; merge-preserve only re-adds a field).
- `adr/0004-structured-primary-agent-logging.md` / `adr/0023-record-exactly-once-lifecycle-logging.md` — no lifecycle events added or removed; stale-contextvar OAuth logging is out of scope (follow-up).
- `adr/0005-use-sub-agent-supervisor-boundary.md` — background-job execution path unchanged.
- Others (0001–0003, 0006–0018, 0020–0022, 0024–0028) — reviewed; no interaction with OAuth token lifecycle or session registration.

## New Durable ADRs Created

- `adr/0029-preserve-oauth-refresh-tokens-across-silent-refreshes.md` — storage merge-preserve contract + `RefreshPreservingOAuthProvider` subclass (design D1/D2).
- `adr/0030-noninteractive-long-lived-mcp-oauth-sessions.md` — interactive authorization is ephemeral; long-lived MCP sessions always run non-interactive providers with clean `needs_auth` degradation (design D3 + D4's durable degradation principle; D4's error-string mechanics remain tactical, in tasks).
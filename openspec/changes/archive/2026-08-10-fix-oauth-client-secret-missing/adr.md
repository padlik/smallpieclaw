# ADR Review Manifest

- Status: completed
- Review date: 2026-08-09

## Review Summary

ADR review completed for this change.

## In-Force ADRs Reviewed

- ADR-0001 through ADR-0019 — reviewed for relevance. None relate to OAuth token storage or the MCP SDK's `OAuthClientProvider` integration. No supersession needed.

## New Durable ADRs Created

- None — no major durable architectural decisions were introduced. This change fixes a bug (missing `token_endpoint_auth_method` in the pre-seeded `OAuthClientInformationFull`) via two tactical implementation choices: a defaulted constructor param on `FileTokenStorage`, and including the value in the pre-seed. These are scoped to `FileTokenStorage` and do not establish a new pattern, boundary, or cross-cutting commitment.

  Note: an earlier draft of this change also sourced the value from `client_metadata` in `build()` and repaired the cached return path via fill-if-None. Both were removed after a post-implementation code review proved them inert — see `review-log.md`. Neither reached the shipped implementation, so neither bears on this ADR review.
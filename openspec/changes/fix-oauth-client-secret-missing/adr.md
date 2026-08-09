# ADR Review Manifest

- Status: completed
- Review date: 2026-08-09

## Review Summary

ADR review completed for this change.

## In-Force ADRs Reviewed

- ADR-0001 through ADR-0019 — reviewed for relevance. None relate to OAuth token storage or the MCP SDK's `OAuthClientProvider` integration. No supersession needed.

## New Durable ADRs Created

- None — no major durable architectural decisions were introduced. This change fixes a bug (missing `token_endpoint_auth_method` in the pre-seeded `OAuthClientInformationFull`) via tactical implementation choices (defaulted constructor param, fill-if-None on cached path, sourcing from `client_metadata`). These are scoped to `FileTokenStorage` and do not establish a new pattern, boundary, or cross-cutting commitment.
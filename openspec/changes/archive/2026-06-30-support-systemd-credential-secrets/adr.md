# ADR Review Manifest

- Status: completed
- Review date: 2026-06-29

## Review Summary

ADR review completed for this change. The change introduces a durable configuration and deployment boundary: production credentials should be represented as provider-level defaults and loaded from files where possible, especially through `systemd --user` credentials.

## In-Force ADRs Reviewed

- None - `<repo>/adr/` had no in-force ADRs before this change.

## New Durable ADRs Created

- `adr/0001-use-file-backed-provider-secrets.md` - records the decision to use provider-level defaults and explicit file-backed secret fields for production credentials.

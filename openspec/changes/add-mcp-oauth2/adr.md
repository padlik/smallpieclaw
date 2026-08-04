# ADR Review Manifest

- Status: completed
- Review date: 2026-08-04

## Review Summary

ADR review completed for this change. No major durable architectural decisions were introduced. The design's decisions are feature-specific implementation choices (OAuth flow mechanics, callback server lifecycle, token storage location) that do not establish cross-cutting architectural patterns or boundaries affecting future changes beyond this one.

The token storage location (`$XDG_STATE_HOME/<name>/mcp_tokens/` via `xdg_paths().mcp_tokens_dir`) is an application of ADR-0019's commitment #3 ("future storage additions must use an XDG bucket"), not a new architectural decision. The `mcp_tokens_dir` field on `XDGPaths` is an implementation detail of that existing ADR.

## In-Force ADRs Reviewed

- ADR-0003: Use TOML for agent-scoped vault files (Accepted, supersedes ADR-0002) — OAuth client_id/secret live in config.toml, not the vault; OAuth tokens are agent-obtained runtime credentials, not operator-provided secrets, so vault integration is deferred.
- ADR-0019: Use XDG Base Directory Specification for all agent storage (Accepted) — token storage follows this ADR: STATE bucket (secret-like, like `secrets.toml`), path resolved via `xdg.py`, directory created in `main.py`.

## New Durable ADRs Created

- None — no major durable architectural decisions were introduced.
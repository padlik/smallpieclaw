# ADR Review Manifest

- Status: completed
- Review date: 2026-08-07

## Review Summary

ADR review completed for this change.

## In-Force ADRs Reviewed

- ADR-0001 through ADR-0019 (all in-force, none superseded by this change). None are relevant to OAuth/MCP transport — they cover secrets, vault, logging, sub-agents, nsjail, XDG, and tool execution. No in-force ADR is revisited or diverged from.

## New Durable ADRs Created

- None — no major durable architectural decisions were introduced. The design decisions (D1-D7) are tactical implementation details about how to trigger the SDK's lazy 401-triggered OAuth flow within `_run_oauth_flow`. They don't establish a new pattern, boundary, or contract that would govern future changes.
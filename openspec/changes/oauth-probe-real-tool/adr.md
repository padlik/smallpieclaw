# ADR Review Manifest

- Status: completed
- Review date: 2026-08-08

## Review Summary

ADR review completed for this change.

## In-Force ADRs Reviewed

- ADR-0001 through ADR-0019 — reviewed all 19 repository-level ADRs. None govern the MCP OAuth probe tool discovery mechanism or transport. No in-force ADR conflicts with this change.

## New Durable ADRs Created

- None — no major durable architectural decisions were introduced. This change is a confined bug fix to the proactive OAuth probe method (replacing dummy-tool POST with tools/list → real-tool tools/call discovery). It does not establish a new pattern, technology choice, or architectural boundary that would affect future changes beyond this one.
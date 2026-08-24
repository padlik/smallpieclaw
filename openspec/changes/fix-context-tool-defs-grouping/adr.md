# ADR Review Manifest

- Status: completed
- Review date: 2026-08-24

## Review Summary

ADR review completed for this change. No major durable architectural decisions were introduced — all three changes are tactical bugfixes that mirror existing patterns (the `/mcp on` tool registration pattern, the `dataclass_replace` idle-transition pattern from ADR-0022, and a name-set classification fallback). The push-model context monitor architecture from ADR-0022 is extended with partial refresh, not replaced.

## In-Force ADRs Reviewed

- ADR-0022: Use push-model context monitor for context-window tracking — this change fixes the tool-defs grouping and adds partial snapshot refresh outside the ReAct loop. The push model itself is unchanged.

## New Durable ADRs Created

- None — no major durable architectural decisions were introduced.
# ADR Review Manifest

- Status: completed
- Review date: 2026-08-17

## Review Summary

ADR review completed for this change. All 19 existing repository-level ADRs were reviewed. Three are directly in-scope as honored invariants (ADR-0004, ADR-0006, ADR-0009) and are cited in design.md's "ADR Context" section. No in-force ADRs need supersession.

## In-Force ADRs Reviewed

- ADR-0004 (structured-primary-agent-logging) — `LogEvent` taxonomy and structlog dual-sink remain unchanged; no new subscribers or Telegram wiring added. Honored.
- ADR-0006 (source-categories-for-agent-visibility) — `on_tool_trace` hook remains unwired for the main agent; sub-agent registry use only. Honored.
- ADR-0009 (native-tool-calling) — tool-calling mechanism unchanged; only the progress-string content at the emission site is modified. Honored.
- ADR-0001 through ADR-0019 — reviewed for relevance; none are superseded or diverged from by this change.

## New Durable ADRs Created

- None — no major durable architectural decisions were introduced. All 9 design decisions are tactical UI/UX implementation details scoped to the `_ProgressPanel` class and the `fmt_tool_brief()` function. They do not establish patterns, boundaries, or contracts that would constrain future changes beyond this one.
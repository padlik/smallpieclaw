# ADR Review Manifest

- Status: completed
- Review date: 2026-08-19

## Review Summary

ADR review completed for this change. One durable architectural decision meets the bar: removing `fallback_models` (established by ADR-0007) and replacing the single static `ctx_max_tokens` with per-model context window awareness. This supersedes ADR-0007's `RuntimeOptions.fallback_models` trichotomy.

## In-Force ADRs Reviewed

- ADR-0007: Use AgentRuntime for agent execution construction (established `RuntimeOptions.fallback_models` — now superseded)
- ADR-0001 through ADR-0019: reviewed for relevance; none other affected by this change

## New Durable ADRs Created

- ADR-0020: Remove fallback_models and add per-model context window (`adr/0020-remove-fallback-models-add-per-model-context-window.md`) — supersedes ADR-0007. Records the removal of the fallback model chain, the re-homing of vision routing onto an all-models scan, and the introduction of per-model `context_window` for compaction.
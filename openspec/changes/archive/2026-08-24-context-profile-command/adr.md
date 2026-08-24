# ADR Review Manifest

- Status: completed
- Review date: 2026-08-23

## Review Summary

ADR review completed for this change. One durable architectural decision was identified: the push-model context monitoring pattern. A new repository-level ADR was created to record it.

## In-Force ADRs Reviewed

- ADR-0003: Use TOML vault format
- ADR-0004: Structured primary agent logging
- ADR-0005: Use sub-agent supervisor boundary
- ADR-0006: Use source categories for agent visibility
- ADR-0007: Use agent runtime for construction
- ADR-0008: Use facade handler package for builtin tools
- ADR-0009: Native tool calling
- ADR-0010: Zone-based file access control
- ADR-0011: Per-prompt approval scope
- ADR-0012: Use nsjail for shell isolation (amended by ADR-0016, ADR-0018)
- ADR-0013: Use ULID for globally unique prompt IDs
- ADR-0014: Use dual-write archive for prompt registry
- ADR-0015: Nsjail state outside sandbox write scope
- ADR-0016: Remove project dir from nsjail sandbox (amended by ADR-0018)
- ADR-0017: Mount session logs readonly in nsjail
- ADR-0018: Mount default trusted zones into nsjail
- ADR-0019: XDG base directory layout for agent storage
- ADR-0020: Remove fallback models, add per-model context window (partially supersedes ADR-0007)
- ADR-0021: Use disk-persisted checkpoints for LLM error recovery

## New Durable ADRs Created

- ADR-0022: Use push-model context monitor for context-window tracking — records the decision to use a push model (ReAct loop publishes snapshot each turn via reference swap) rather than a pull model (on-demand snapshot). This is the architectural foundation for future context budgeting and automated event triggers. Also documents the `maybe_compact` tool-def visibility fix as an amendment to ADR-0020's compaction model.
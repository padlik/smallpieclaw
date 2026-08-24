# ADR Review Manifest

- Status: completed
- Review date: 2026-08-21

## Review Summary

ADR review completed for this change. One new durable architectural decision was identified: the disk-persisted checkpoint store for LLM error recovery.

## In-Force ADRs Reviewed

- ADR-0001: Use file-backed provider secrets (Accepted)
- ADR-0003: Use TOML for agent-scoped vault files (Accepted, supersedes ADR-0002)
- ADR-0004: Use structlog for structured-primary agent logging (Accepted)
- ADR-0005: Use SubAgentSupervisor as the sub-agent supervision boundary (Accepted)
- ADR-0006: Use source categories for running agent visibility and capacity (Accepted)
- ADR-0007: Use AgentRuntime for agent execution construction (Accepted, partially superseded by ADR-0020)
- ADR-0008: Use a façade + handler-module package for built-in tools (Accepted)
- ADR-0009: Use native tool calling as primary path with text-based JSON fallback (Accepted)
- ADR-0010: Use zone-based access control for file operations (Accepted)
- ADR-0011: Use per-prompt scope for operator approval grants (Accepted, partially superseded by ADR-0012)
- ADR-0012: Use nsjail for shell command isolation with configurable confirmation (Accepted, partially supersedes ADR-0011, partially superseded by ADR-0016)
- ADR-0013: Use ULID for globally-unique prompt IDs (Accepted)
- ADR-0014: Use dual-write archive for prompt registry search and bounded memory (Accepted — referenced as pattern for atomic writes)
- ADR-0015: nsjail sandbox configuration state must reside outside the sandbox's write scope (Accepted)
- ADR-0016: Remove project_dir mount from nsjail sandbox (Accepted, partially supersedes ADR-0012, partially superseded by ADR-0018)
- ADR-0017: Mount session_logs read-only inside the nsjail sandbox (Accepted)
- ADR-0018: Mount default-trusted zones into nsjail (Accepted, partially supersedes ADR-0016)
- ADR-0019: Use XDG Base Directory Specification for all agent storage paths (Accepted)
- ADR-0020: Remove fallback_models and add per-model context window (Accepted, partially supersedes ADR-0007)

## New Durable ADRs Created

- ADR-0021: Use disk-persisted checkpoints for LLM error recovery (`adr/0021-use-disk-persisted-checkpoints-for-llm-error-recovery.md`) — establishes the `data/run_checkpoints/` persistence layer with atomic writes, the write-on-error/delete-on-success/survive-timeout lifecycle contract, and the no-automatic-cleanup decision. Future features that need to preserve run state across failures can reuse the `CheckpointStore` infrastructure.
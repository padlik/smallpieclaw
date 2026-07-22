# ADR Review Manifest

- Status: completed
- Review date: 2026-07-21

## Review Summary

ADR review completed for this change. One durable architectural decision was identified: the per-prompt approval scope model (shifting `auto_approve_tools` from process-lifetime to per-prompt, and sharing the set with sub-agents). This is a contract change on the approval model that future changes touching approvals must respect.

## In-Force ADRs Reviewed

- ADR-0001: Use file-backed provider secrets
- ADR-0002: Vault secret manager
- ADR-0003: Use TOML vault format
- ADR-0004: Structured primary agent logging
- ADR-0005: Use SubAgentSupervisor as the sub-agent supervision boundary
- ADR-0006: Use source categories for agent visibility
- ADR-0007: Use AgentRuntime for agent construction
- ADR-0008: Use facade handler package for builtin tools
- ADR-0009: Native tool calling
- ADR-0010: Zone-based file access control

None superseded by this change. ADR-0010 (zone-based file access control) is explicitly preserved — the approve-all check short-circuits the zone-triggered confirmation, not the zone classification itself.

## New Durable ADRs Created

- ADR-0011: Use per-prompt scope for operator approval grants (`adr/0011-per-prompt-approval-scope.md`) — establishes that approval grants are per-prompt (cleared at `run()` end) and shared between the main agent and its sub-agents via a per-prompt reference on the shared `BuiltinExecutor`.
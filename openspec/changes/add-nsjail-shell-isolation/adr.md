# ADR Review Manifest

- Status: completed
- Review date: 2026-07-22

## Review Summary

ADR review completed for this change. One new durable ADR was created: ADR-0012, which supersedes the main-agent shell-never-auto-approved invariant from ADR-0011.

## In-Force ADRs Reviewed

- ADR-0001: Use file-backed provider secrets for production credentials (Accepted)
- ADR-0003: Use TOML for agent-scoped vault files (Accepted, supersedes ADR-0002)
- ADR-0004: Use structlog for structured-primary agent logging (Accepted)
- ADR-0005: Use SubAgentSupervisor as the sub-agent supervision boundary (Accepted)
- ADR-0006: Use source categories for running agent visibility and capacity (Accepted)
- ADR-0007: Use AgentRuntime for agent execution construction (Accepted)
- ADR-0008: Use a façade + handler-module package for built-in tools (Accepted)
- ADR-0009: Use native tool calling as primary path with text-based JSON fallback (Accepted)
- ADR-0010: Use zone-based access control for file operations (Accepted)
- ADR-0011: Use per-prompt scope for operator approval grants (Accepted — partially superseded by ADR-0012)

## New Durable ADRs Created

- ADR-0012: Use nsjail for shell command isolation with configurable confirmation (`adr/0012-use-nsjail-for-shell-isolation.md`). Supersedes ADR-0011's main-agent shell-never-auto-approved invariant. The sub-agent fail-closed half of ADR-0011 is preserved.
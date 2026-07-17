# ADR Review Manifest

- Status: completed
- Review date: 2026-07-17

## Review Summary

ADR review completed for this change. One major durable architectural decision was identified and recorded.

## In-Force ADRs Reviewed

- ADR-0001: Use file-backed provider secrets for production credentials *(superseded by ADR-0003)*
- ADR-0002: Use agent-scoped vault for centralized secret storage *(superseded by ADR-0003)*
- ADR-0003: Use TOML for agent-scoped vault files
- ADR-0004: Use structlog for structured-primary agent logging
- ADR-0005: Use SubAgentSupervisor as the sub-agent supervision boundary
- ADR-0006: Use source categories for running agent visibility and capacity
- ADR-0007: Use AgentRuntime for agent execution construction
- ADR-0008: Use a façade + handler-module package for built-in tools
- ADR-0009: Native tool calling

None of the in-force ADRs (0003–0009) govern file path access control. The change is consistent with ADR-0008 (new `builtin_tools/access_control.py` module follows the handler-subpackage pattern).

## New Durable ADRs Created

- `adr/0010-zone-based-file-access-control.md` — Establishes `TrustedZoneChecker` as the canonical access-control gate for all `file_*` built-in tool operations, defines the four-zone classification model (internal / trusted / request-grant / unrecognised), and mandates `os.path.realpath()` for all path resolution.

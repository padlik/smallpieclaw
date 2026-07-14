# ADR Review Manifest

- Status: completed
- Review date: 2026-07-14

## Review Summary

ADR review completed for this change. The design introduces one durable architectural
commitment — the façade + handler-module package pattern for built-in tools — which is
recorded as a new repository-level ADR. No in-force ADR needs supersession; the design is
coherent with the currently in-force set.

## In-Force ADRs Reviewed

Supersession graph walked (0001→0002→0003). Currently in force:

- ADR-0003 — Use TOML for agent-scoped vault files (constrains the `secret_get` handler).
- ADR-0004 — Use structlog for structured-primary agent logging (lifecycle
  `TOOL_START`/`TOOL_END`/`TOOL_FAILED` events must be preserved on the façade).
- ADR-0005 — Use SubAgentSupervisor as the sub-agent supervision boundary (the agents handler
  must keep delegating to `_supervisor`).
- ADR-0006 — Use source categories for running agent visibility and capacity (unaffected).
- ADR-0007 — Use AgentRuntime for agent execution construction (the `_sub_agent_factory` the
  executor reads is runtime-produced; this split consumes that seam without changing it).

Superseded (historical only): ADR-0001, ADR-0002.

## New Durable ADRs Created

- `adr/0008-use-facade-handler-package-for-builtin-tools.md` — commits to the thin
  `BuiltinExecutor` façade plus a per-concern `builtin_tools/` handler package with
  owner-back-reference late binding and name-keyed dispatch registries; defers
  `ConfirmationCoordinator` while keeping the confirmation seam clean.

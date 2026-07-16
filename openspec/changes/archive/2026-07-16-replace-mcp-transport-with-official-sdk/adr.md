# ADR Review Manifest

- Status: completed
- Review date: 2026-07-16

## Review Summary

ADR review completed for this change. All 7 in-force ADRs were reviewed. No new durable architectural decisions were introduced — the change replaces a hand-rolled transport implementation with the official `mcp` Python SDK, which is a dependency upgrade, not a new architectural pattern or boundary. The async bridge (event loop thread + session-runner pattern) is a tactical implementation detail scoped to `mcp_client.py` internals and does not establish a long-term commitment affecting future changes.

## In-Force ADRs Reviewed

- **ADR-0003**: Use TOML for agent-scoped vault files — no impact; config format unchanged
- **ADR-0004**: Use structlog for structured-primary agent logging — no impact; logging stays in `mcp_client.py`
- **ADR-0005**: Use SubAgentSupervisor as the sub-agent supervision boundary — no impact; MCP is not sub-agent related
- **ADR-0006**: Use source categories for running agent visibility and capacity — no impact
- **ADR-0007**: Use AgentRuntime for agent execution construction — no impact; MCPManager is constructed in `main.py` as before
- **ADR-0008**: Use a façade + handler-module package for built-in tools — no impact; MCP tools are not built-in tools
- **ADR-0009**: Use native tool calling as primary path with text-based JSON fallback — no impact; MCP tool dispatch in `react_loop.py` is unchanged

## New Durable ADRs Created

- None — no major durable architectural decisions were introduced.

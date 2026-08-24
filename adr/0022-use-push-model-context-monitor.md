# Use push-model context monitor for context-window tracking

## Status

Accepted

## Date

2026-08-23

## Context

The agent's context window is consumed by three channels — system prompt, chat history, and tool definitions — but only the first two are measured. The compaction system (`maybe_compact`, established in ADR-0020) computes its threshold from `estimate_messages_tokens(messages, system)` and is blind to the tool-definition token cost. On the native tool-calling path (ADR-0009), all 21 built-in tools plus every registered MCP tool are injected into the LLM payload with no filtering, and this cost is invisible to compaction.

The operator has no visibility into context consumption by category. The existing `/show_ctx` command dumps the full system prompt as a file but does not break down consumption or surface the hidden tool-definition cost. There is no mechanism to identify MCP servers that consume a disproportionate share of the context window.

This change introduces a `ContextMonitor` as the foundation for future context budgeting and automated event triggers. The monitor must be continuously tracking (not on-demand), lightweight (not a performance burden), and accessible from both the Telegram interface and built-in tools.

## Considered Options

- **Push model (chosen):** The ReAct loop publishes a `ContextSnapshot` to the `ContextMonitor` each turn via a reference swap (no deep copy, no lock). The monitor stores the latest snapshot. `/context` and `context_profile` read from it. The monitor is always current and can fire events when a push crosses a threshold.
- **Pull model:** The monitor builds a snapshot on-demand when `/context` or the tool is called, reading from `AgentController` state. Rejected — the monitor would be "always computing on demand," not "always tracking." Future event triggers would need a separate mechanism to detect threshold crossings, since there's no continuous data stream.
- **Deep copy under lock:** The monitor stores a consistent snapshot via deep copy with a threading lock. Rejected — the snapshot is a diagnostic view, not a transactional read. A slightly stale snapshot is acceptable, and the lock adds unnecessary overhead to the hot path (every turn).

## Decision

Use a push model: the ReAct loop publishes a `ContextSnapshot` to the `ContextMonitor` each turn via a lightweight reference swap. The monitor lives on `AgentController` (shared state, persists between runs). The snapshot stores token counts and metadata — not copies of messages or tool definitions. Reads from the Telegram event loop are concurrent with publishes from the agent worker thread; Python's GIL makes the reference swap atomic at the bytecode level, so no lock is needed.

The monitor tracks four top-level categories (system prompt, chat history, tool definitions, completion reserve) and one sub-category (tool definitions grouped by MCP server). It computes `danger_level` (safe / approaching / danger) from the real headroom (accounting for tool defs).

Additionally, `maybe_compact` gains an optional `tool_defs_tokens` parameter (default 0 for backward compatibility) so the compaction threshold accounts for the real payload size. This amends ADR-0020's compaction model but does not supersede it — the threshold formula is extended, not replaced.

## Consequences

- Good, because the monitor is always current and lightweight — a reference swap per turn, not a deep copy.
- Good, because the monitor is the foundation for future context budgeting and automated event triggers (the push stream enables threshold-crossing detection).
- Good, because the `maybe_compact` fix prevents silent context overflow from tool definitions that the compaction logic couldn't see.
- Good, because MCP servers with high tool-def cost are visible via the per-server grouping, enabling operators to disable wasteful servers.
- Bad, because the snapshot may be one turn stale when read during a run (reference swap without lock). Accepted — the snapshot includes `turn` and `is_live` fields so the operator knows how fresh it is.
- Bad, because `estimate_tokens(json.dumps(tool_defs))` runs each turn. Accepted — the cost is negligible compared to the LLM call itself, and tool defs are built once per run so the result could be cached.

## Constraints

- The `ContextMonitor` tracks the **MAIN** runtime only. Sub-agent `ReactContext` instances must **not** share the main monitor instance; they receive `context_monitor=None` by design, so their per-turn publishes are no-ops. This prevents sub-agent snapshots from clobbering the main run's live snapshot. See ADR-0007 (`RuntimeProfile`) for runtime construction boundaries.

## Follow-up

- Future changes can add system prompt section-level tracking, event triggers on danger level, and `top_tools` filtering on the native tool-calling path.
# Use AgentRuntime for agent execution construction

## Status

Accepted

## Date

2026-07-13

## Supersedes

None

## Context

Agent execution currently shares one ReAct loop but constructs controllers, sub-agent runners, LLM clients, memory layers, trace/cancel wiring, and context payloads through several scattered paths. The previous changes established `SubAgentSupervisor` as the supervision boundary and source categories as the visibility/capacity model, leaving construction as the remaining duplicated runtime concern.

## Considered Options

- Keep construction spread across `AgentController`, `SubAgentRunner`, `sub_agent_factory`, and `PlanExecutor` until the builtin split.
- Merge `AgentController` and `SubAgentRunner` into one mode-flag class.
- Introduce `AgentRuntime` as a construction boundary with explicit profiles and options, while keeping supervision and orchestration separate.

## Decision

Use `AgentRuntime` as the construction boundary for agent executions. `RuntimeProfile` describes construction policy; `RuntimeOptions` carries per-execution construction knobs. The runtime may build existing frontend products such as `AgentController` or `SubAgentRunner`-compatible objects, but it must preserve the consumer-facing surface expected by supervisors, planners, and tests.

`RuntimeProfile` remains separate from `SubAgentRecord.source`: profiles describe construction behavior, while sources describe operator visibility and capacity semantics.

## Consequences

- Good, because construction behavior has one explicit place to preserve model overrides, fallback behavior, memory wiring, trace/cancel wiring, and prompt variants.
- Good, because future runtime/profile changes have a stable vocabulary before built-in tools are split.
- Good, because supervision, visibility, and orchestration remain separately owned by the components established in earlier ADRs.
- Bad, because this adds another abstraction layer that must not grow into a second executor or orchestration engine.
- Bad, because golden-equivalence tests are required to prevent subtle regressions in post-init wiring, callback ordering, model active-index restoration, and context assembly.
- Bad, because centralizing trace/cancel wiring in the runtime makes it responsible for preserving ADR-0004's explicit trace propagation and thread/executor trace-identity binding guarantees.
- Neutral, because future `AgentRuntime` work may choose whether to return concrete frontends or wrapper products, provided the committed surface remains compatible.

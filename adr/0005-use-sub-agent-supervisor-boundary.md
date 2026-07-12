# Use SubAgentSupervisor as the sub-agent supervision boundary

## Status

Accepted

## Date

2026-07-12

## Supersedes

None

## Context

Sub-agent spawning currently mixes the model-facing `spawn_agent` tool contract with background execution supervision. The same method validates tool arguments, builds context payloads, creates registry records, submits thread-pool work, saves context, routes scheduler callbacks, formats notifications, signals result availability, and performs cleanup.

This makes the spawn path difficult to maintain and blocks later refactors such as introducing a shared runtime builder or splitting built-in tools. Scheduler launches also pass internal control values through the same dictionary used for model-facing tool arguments, which blurs trust and ownership boundaries.

## Considered Options

- Keep supervision inside `builtin_executor._exec_spawn_agent` and only move unrelated built-in tools later.
- Introduce a full `AgentRuntime` and profile system first.
- Extract a focused `SubAgentSupervisor` boundary first, while keeping `react_loop`, runner construction, runtime profiles, and plan execution unchanged.

## Decision

Use `SubAgentSupervisor` as the first-class boundary for background sub-agent supervision. The model-facing `spawn_agent`/`_exec_spawn_agent` path remains a compatibility shim for argument parsing, validation, friendly policy errors, and `agent_id` return behavior. The supervisor owns registration, background submission, result signaling, context-save lifecycle, scheduler callback delivery, notification delivery, and cleanup.

Scheduler/internal control values must flow through supervisor-owned per-submission options rather than through model-facing `spawn_agent` arguments.

## Consequences

- Good, because the spawn tool becomes a thinner model-facing adapter instead of a mini-runtime.
- Good, because scheduler callback routing is separated from model-provided arguments.
- Good, because later AgentRuntime/profile and built-in-tool-splitting changes have a clearer boundary to build on.
- Good, because per-submission supervision options preserve callback isolation for concurrent scheduled jobs.
- Bad, because this introduces a new architectural component before the broader runtime model is finalized.
- Bad, because behavior-preserving extraction of threaded lifecycle code requires careful tests around cancellation, timeout, registry records, and cleanup.
- Bad, because moving thread-pool submission into the supervisor makes it responsible for preserving ADR-0004's requirement that structured logging/trace identity be bound at each background thread or executor entry.
- Neutral, because running-agent visibility and cap semantics remain intentionally deferred to a later decision.

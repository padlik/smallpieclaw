# Use a façade + handler-module package for built-in tools

## Status

Accepted

## Date

2026-07-14

## Supersedes

None

## Context

`builtin_executor.py` had grown to 2713 lines: one `BuiltinExecutor` class implementing 15
built-in tools, holding the `BUILTIN_TOOLS` descriptor dict, dispatching via `if/elif`
chains, owning the cross-cutting confirmation machinery (interactive staging + a headless
sub-agent → Telegram operator bridge), and reading eight collaborators wired after
construction. This violated the project convention against large files/superclasses and made
the module hard to read, review, and extend. ADR-0007 established `AgentRuntime` as the
construction boundary specifically to make this split safe; ADR-0005 keeps sub-agent
supervision behind `SubAgentSupervisor`; ADR-0004 requires structured lifecycle logging to be
preserved.

## Considered Options

- **Mixins**: split tool groups into base classes composed into `BuiltinExecutor`. Keeps
  `self.*` access for free but adds an inheritance tree (superclasses) and only cosmetically
  dissolves the god class.
- **Pure delegation with self-contained handlers**: each handler owns its collaborators.
  Impossible without change, because 8 collaborators are wired post-construction (ADR-0007
  seam) and would be `None` at handler init; handlers would need an owner reference anyway.
- **Hybrid façade + handler package**: a thin `BuiltinExecutor` façade retains the public
  surface and all confirmation state; per-concern handler classes in a `builtin_tools/`
  package hold a live owner back-reference and read late-bound collaborators at call time;
  `if/elif` is replaced by two name-keyed registry dicts.

## Decision

Adopt the hybrid façade + handler-module package. Tool bodies move into `builtin_tools/`
grouped by concern (shell, filesystem, sub-agents, memory/graph, secrets/logging) plus
stateless leaf modules (descriptors, patterns, text utils, log-query helpers, context I/O).
`BuiltinExecutor` stays the façade: it constructs the handlers, owns the confirmation
lifecycle, exposes the public API and the pinned reach-in forwarders
(`_exec_spawn_agent`/`_exec_get_agent_result`/inline `_exec_schedule`), and routes through
`_exec_table`/`_run_table`. Handlers never snapshot the 8 late-bound collaborators; they read
them via the owner at call time. New built-in tools are added by registering a descriptor, a
handler method, and a table entry.

Extraction of confirmation into a separate `ConfirmationCoordinator` is explicitly deferred to
a follow-on change; this decision only commits to keeping the confirmation seam clean
(handlers use `_requires_confirmation`/`confirm`/`cancel` only; `_run_table` is the sole
phase-2 route; logging stays module-level).

## Consequences

- Good, because the module is decomposed into small per-concern files without introducing a
  superclass, and there is now one documented pattern for adding built-in tools.
- Good, because it stays coherent with ADR-0007 (reads the runtime-produced
  `_sub_agent_factory`), ADR-0005 (agents handler still delegates to `_supervisor`), and
  ADR-0004 (lifecycle logging preserved on the façade).
- Bad, because the god-object *state* remains centralized on the façade and handlers keep an
  owner back-reference — this is decoupling of code size, not of coupling; full decoupling
  waits on the deferred `ConfirmationCoordinator`.
- Bad, because it imposes ongoing discipline: collaborator imports must stay function-local
  and `builtin_tools/__init__.py` must stay light to avoid import cycles, and handlers must
  read late-bound attributes at call time to avoid capturing `None`.
- Neutral, because the clean confirmation seam lets a later change lift confirmation into its
  own object with a mechanical move rather than a rewrite.

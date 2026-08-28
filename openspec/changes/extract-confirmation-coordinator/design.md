## Context

The agent's confirmation infrastructure currently spans two independent signaling systems that share state through fragile cross-references:

1. **ConfirmationManager** (`confirmation.py`) — owned by `AgentController`, handles three depth-0 flows: tool confirmation, step extension, and LLM error retry. Uses `threading.Event` + result dict + token lifecycle.

2. **Headless bridge** (on `BuiltinExecutor`) — handles sub-agent (depth ≥ 1) tool confirmations. Uses a *separate* `threading.Event` + result dict, but stages tools into the *same* `_pending` dict as system 1, and reads `auto_approve_tools` via a `_prompt_approval_set` reference that points to `ConfirmationManager`'s set.

The entanglement causes a race condition: `cb_subagent_confirm` in Telegram mutates `auto_approve_tools` via direct field access (`iface.agent._confirmation.auto_approve_tools.add(tool_name)`) separately from the event signal, creating a window where a concurrent sub-agent thread can read the set between the two operations.

Three archived changes (`cleanup-builtin-executor-facade`, `split-builtin-executor-modules`, `prompt-scoped-approvals-and-subagent-control`) deferred this extraction, gated on two decisions now resolved:
- Security review: nsjail shell isolation (ADR-0012) provides kernel-level sandboxing; configurable confirmation mode (`always`/`adaptive`/`never`) is in place.
- Unified approve-all: extend `ConfirmationManager` to absorb the headless flow (single-gate pattern, per research).

### Current architecture (component diagram)

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Telegram Bot (asyncio loop)                                           │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────┐ ┌────────────┐ │
│  │cb_confirm│ │cb_extend │ │cb_retry  │ │cb_subagent   │ │cb_zone_*  │ │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘ └──────┬───────┘ └─────┬──────┘ │
│       │            │            │              │               │       │
│       ▼            ▼            ▼              ▼               │       │
│  agent.resume  agent.resume_  agent.resume_  builtin.signal_   │       │
│    ()           extend()      llm_error()    headless_confirm  │       │
│                                   + agent._confirmation         │       │
│                                   .auto_approve_tools          │       │
│                                   .add()  ◄── RACE             │       │
└───────┼────────────┼────────────┼──────────────┼───────────────┼───────┘
        │            │            │              │               │
════════╪════════════╪════════════╪══════════════╪═══════════════╪═══════
        │            │            │              │               │
┌───────┼────────────┼────────────┼──────────────┼───────────────┼───────┐
│  AgentController (worker thread) │              │               │       │
│       │            │            │              │               │       │
│       ▼            ▼            ▼              │               │       │
│  ┌─────────────────────────────────┐           │               │       │
│  │  ConfirmationManager           │           │               │       │
│  │  _confirm_events/_results       │           │               │       │
│  │  _extend_events/_results        │           │               │       │
│  │  _retry_events/_results          │           │               │       │
│  │  auto_approve_tools ◄── shared   │           │               │       │
│  └─────────────────────────────────┘           │               │       │
│       ▲                                        │               │       │
│       │ _prompt_approval_set = auto_approve ───┘               │       │
│       │ ref set at run start, None at end                       │       │
│                                                                │       │
│  ┌─────────────────────────────────────────────────────────────────────┐ │
│  │  BuiltinExecutor                                                   │ │
│  │  _pending ◄── SHARED (both systems stage here)                     │ │
│  │  _headless_confirm_events ◄── SEPARATE from ConfirmationManager    │ │
│  │  _headless_confirm_results ◄── SEPARATE                           │ │
│  │  _prompt_approval_set ◄── ref to auto_approve_tools               │ │
│  │  _subagent_confirm_prompt_fn ◄── set by main.py                   │ │
│  │  _subagent_confirm_timeout ◄── 120s                              │ │
│  │  _zone_paths / _zone_trackers                                     │ │
│  │  confirm(token) / cancel(token)                                   │ │
│  └───────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────┘
```

### Target architecture (component diagram)

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Telegram Bot (asyncio loop)                                           │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────┐ ┌────────────┐ │
│  │cb_confirm│ │cb_extend │ │cb_retry  │ │cb_subagent   │ │cb_zone_*  │ │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘ └──────┬───────┘ └─────┬──────┘ │
│       │            │            │              │               │       │
│       ▼            ▼            ▼              ▼               │       │
│  agent.resume  agent.resume_  agent.resume_  coordinator.     │       │
│    ()           extend()      llm_error()    signal_headless_  │       │
│                                   confirmation()  ◄── ATOMIC   │       │
│                                                (approve_all    │       │
│                                                 + set event    │       │
│                                                 in one call)  │       │
└───────┼────────────┼────────────┼──────────────┼───────────────┼───────┘
        │            │            │              │               │
════════╪════════════╪════════════╪══════════════╪═══════════════╪═══════
        │            │            │              │               │
┌───────┼────────────┼────────────┼──────────────┼───────────────┼───────┐
│  AgentController (worker thread) │              │               │       │
│       │            │            │              │               │       │
│       ▼            ▼            ▼              │               │       │
│  ┌──────────────────────────────────────────────┐               │       │
│  │  ConfirmationManager (extended)              │               │       │
│  │  _confirm_events/_results                    │               │       │
│  │  _extend_events/_results                     │               │       │
│  │  _retry_events/_results                      │               │       │
│  │  _headless_confirm_events ◄── NEW (absorbed)│               │       │
│  │  _headless_confirm_results ◄── NEW (absorbed)│              │       │
│  │  default_headless_timeout: 120s ◄── NEW      │               │       │
│  │  auto_approve_tools ◄── SOLE OWNER           │               │       │
│  │                                              │               │       │
│  │  request_headless_confirmation(              │               │       │
│  │    token, tool, desc, prompt_fn,             │               │       │
│  │    timeout, caller_tag) → bool  ◄── NEW      │               │       │
│  │  signal_headless_confirmation(                │               │       │
│  │    token, approved, approve_all,              │               │       │
│  │    tool_name) → bool  ◄── NEW (ATOMIC)       │               │       │
│  └──────────────────────────────────────────────┘               │       │
│       ▲  ▲                                     ▲               │       │
│       │  │ _coordinator = self._confirmation     │               │       │
│       │  │ set at run start, None at end         │               │       │
│  ┌────┼──┼─────────────────────────────────────┼───────────────┼──────┐ │
│  │  BuiltinExecutor                          │  │               │      │ │
│  │  _pending ◄── still shared (staging)      │  │               │      │ │
│  │  _coordinator ◄── ref to ConfirmationMgr │  │               │      │ │
│  │  _subagent_confirm_prompt_fn ◄── stays   │  │               │      │ │
│  │  _zone_paths / _zone_trackers ◄── stays   │  │               │      │ │
│  │  confirm(token) / cancel(token)          │  │               │      │ │
│  │  _headless_confirm_bridge → delegates    │  │               │      │
│  │    wait to coordinator, passes prompt_fn │  │               │      │ │
│  └────────────────────────────────────────────┘  │               │      │ │
└──────────────────────────────────────────────────────────────────────────┘
```

## Goals / Non-Goals

**Goals:**
- Unify all four confirmation signaling flows (tool confirm d0, tool confirm d≥1, step extension, LLM error retry) behind a single `ConfirmationManager` instance.
- Eliminate the `cb_subagent_confirm` race condition by making approve-all atomic (set mutation + event signal in one method call).
- Remove the `_prompt_approval_set` cross-reference indirection — the coordinator owns `auto_approve_tools` directly.
- Keep the coordinator transport-agnostic: no Telegram-specific callbacks stored on it; the prompt function is passed at call time.
- Preserve all externally observable behavior: same Telegram buttons, same prompts, same timeouts, same approve-all semantics.

**Non-Goals:**
- Reducing `BuiltinExecutor.__init__` parameter count (C-07's scope).
- Config migration to typed `ExecutorConfig` (C-17's scope).
- Removing main-agent shell approve-all (separate "shell-guard-v0" change).
- Durable pause/resume with state persistence (threading.Event is sufficient for single-operator assistant).
- Adding confidence-threshold routing or batch review (research-derived patterns not applicable to this single-operator use case).
- Moving `_pending`, `_zone_paths`, or `_zone_trackers` to the coordinator (tool-execution context stays on executor).

## Decisions

### Decision 1: Extend ConfirmationManager in-place (no new class, no rename)

**Choice:** Add `request_headless_confirmation()` and `signal_headless_confirmation()` to the existing `ConfirmationManager` class. No rename, no new file.

**Rationale:** The class already implements the exact Event+dict+token pattern for three flow types. The fourth (headless) uses the same pattern. Creating a separate `ConfirmationCoordinator` class would add indirection with zero behavioral benefit, and renaming would cause import churn across 6+ files with no architectural gain. The research consensus is "one gate, don't create separate coordinators" — extending the existing one IS that single gate.

**Alternatives considered:**
- New `ConfirmationCoordinator` class wrapping both systems — rejected: indirection without benefit, doubles the import surface.
- Rename `ConfirmationManager` → `ConfirmationCoordinator` — rejected: import churn, zero behavioral change.

### Decision 2: Coordinator owns signaling only; executor owns staging and execution

**Choice:** `_pending`, `confirm(token)`, `cancel(token)`, `_zone_paths`, `_zone_trackers` stay on `BuiltinExecutor`. The coordinator owns only Event+result dicts and `auto_approve_tools`.

**Rationale:** Single-responsibility. The coordinator's job is "ask the human, wait for answer, return decision." Tool staging and execution is the executor's domain. Moving `_pending` into the coordinator would make it responsible for tool execution, breaking the boundary. The research pattern (`ApprovalGate.requestApproval → "approved"|"rejected"|"expired"`) confirms this separation: the gate returns a decision; the caller acts on it.

**Alternatives considered:**
- Move `_pending` into coordinator — rejected: couples coordinator to tool execution.

### Decision 3: Run lifecycle stays external (no run-scoping on coordinator)

**Choice:** `agent_controller.py` sets `builtin._coordinator = self._confirmation` at run start, sets it to `None` in the `finally` block. Same pattern as today, just pointing at the coordinator instead of the approval set.

**Rationale:** The coordinator is a long-lived object (created in `AgentController.__init__`). Adding run-scoping adds state management to a class whose job is signaling, not lifecycle. The controller already manages run lifecycle. Swapping the reference from `_prompt_approval_set` to `_coordinator` is a one-line change at each site.

**Alternatives considered:**
- Coordinator tracks run scope internally (`begin_run()`/`end_run()`) — rejected: adds state management to a signaling class.

### Decision 4: Single method for atomic headless approve-all

**Choice:** `signal_headless_confirmation(token, approved, approve_all=False, tool_name="")` — one method that atomically adds to `auto_approve_tools` AND sets the event.

**Rationale:** The approve-all and the signal must be atomic. Splitting them into two methods recreates the race: the caller would need to call both, and a concurrent sub-agent could read the set between the two calls. One method with a flag keeps it atomic. This mirrors how `signal_approve_all` already works on the main-agent path (adds to set + sets event in one call).

**Alternatives considered:**
- Two methods (`signal_headless_confirmation` + `signal_headless_approve_all`) — rejected: recreates the race between set mutation and event signal.

### Decision 5: Prompt function passed at call time, not stored on coordinator

**Choice:** `_subagent_confirm_prompt_fn` stays on `BuiltinExecutor` (set by `main.py` as today). The bridge passes it to `coordinator.request_headless_confirmation(prompt_fn=...)` at call time. The coordinator never stores it.

**Rationale:** The prompt function is a Telegram-specific concern (formatting, sending inline keyboards). Storing it on the coordinator couples it to the UI layer. Passing at call time keeps the coordinator transport-agnostic: "give me a way to ask, I'll wait for the answer."

**Alternatives considered:**
- Move `_subagent_confirm_prompt_fn` to the coordinator — rejected: couples coordinator to Telegram transport.

### Decision 6: Zone context stays on executor

**Choice:** `_zone_paths` and `_zone_trackers` remain on `BuiltinExecutor`. Telegram callbacks still reach them via `builtin._zone_paths.pop(token)`.

**Rationale:** Zone paths and trackers are tool-execution context, not confirmation-signaling state. They're populated at staging time and consumed at execution time. The coordinator doesn't need to know about zones, paths, or grant trackers.

**Alternatives considered:**
- Move to coordinator — rejected: couples coordinator to zone-based file access control.

## Risks / Trade-offs

- **[Risk] Concurrent access to `auto_approve_tools` without explicit lock** → Mitigation: CPython GIL makes single-key set operations (`add`, `in`) atomic. The atomic `signal_headless_confirmation` method does `add` + `event.set()` in sequence — both are GIL-protected, and `event.set()` is the release point. A sub-agent reading `auto_approve_tools` between the two operations would see the tool already added (set mutation happened first), which is the correct behavior (approve-all should take effect immediately). No lock needed.

- **[Risk] Tests directly access `_prompt_approval_set` field** → Mitigation: `test_prompt_approval_ttl.py` and `test_supervisor_prompt_wiring.py` set `executor._prompt_approval_set` directly. These tests must be updated to set `executor._coordinator` instead. The migration is mechanical (replace `_prompt_approval_set` with `_coordinator` and point at the `ConfirmationManager` instance). `test_subagent_approve_all.py` directly mutates `auto_approve_tools` — update to use `signal_headless_confirmation(approve_all=True)`.

- **[Risk] `secrets_log.py:61` calls `_headless_confirm_bridge` directly** → Mitigation: The bridge signature doesn't change; it still lives on `BuiltinExecutor`. Only its internal implementation changes (delegates to coordinator). No caller needs updating.

- **[Trade-off] ConfirmationManager grows from 3 to 4 flow types** → Acceptable: the class is 196 lines; adding ~40 lines for the headless flow keeps it under 250 lines — well within project conventions. The alternative (a separate coordinator class) would split the same pattern across two files.

- **[Behavior change] Orphaned sub-agent fail-closed path changes from "prompt" to "block"** → In the old design, when `_prompt_approval_set` was `None` after run end, the bridge still had its own `_headless_confirm_events` and `_subagent_confirm_prompt_fn` on the executor, so it could still prompt the operator. In the new design, the coordinator owns the prompt/wait machinery — when `_coordinator is None`, the bridge has nothing to delegate to. The bridge SHALL check `self._coordinator is None` at entry and return a fail-closed error immediately without prompting. This is an intentional behavior change: an orphaned sub-agent after run end gets blocked rather than prompting an operator who may not be expecting a prompt from a finished task's sub-agent. The old "prompt" behavior was arguably a bug — there's no live run context to deliver the prompt into.

## Migration Plan

1. Add new methods and state to `ConfirmationManager` (additive — no existing API changes).
2. Add `_coordinator` field to `BuiltinExecutor` (additive).
3. Refactor `_headless_confirm_bridge` to delegate to coordinator (internal change, no signature change).
4. Update `agent_controller.py` run lifecycle wiring (one-line swap at two sites).
5. Update `telegram_callbacks.py` `cb_subagent_confirm` to call coordinator method (removes direct field mutation).
6. Remove old state from `BuiltinExecutor` (`_headless_confirm_events`, `_headless_confirm_results`, `_prompt_approval_set`, `_subagent_confirm_timeout`).
7. Update tests to use new APIs.
8. Run `make check` to verify.

**Rollback:** Steps 1-3 are additive and can be reverted by removing the new code. Steps 4-6 are the breaking changes — revert to restore `_prompt_approval_set` wiring and direct field mutation. No data migration needed (all state is in-memory).

## Open Questions

- **ADR-0011 supersession**: ADR-0011 ("Use per-prompt scope for operator approval grants") established `_prompt_approval_set` as the mechanism for sharing `auto_approve_tools` with sub-agents. This change replaces that mechanism with `_coordinator`. ADR-0011's *decision* (per-prompt scope, shared set) is preserved — only the *mechanism* changes. The ADR step should record a superseding ADR that preserves the per-prompt TTL decision while updating the implementation mechanism. Note: the existing main spec at line 71 references "ADR-0011's invariant that 'shell is never auto-approved'" — that is an amendment to ADR-0011 made by the nsjail shell isolation change, not ADR-0011's subject. ADR-0011's actual title and decision are confirmed as "Use per-prompt scope for operator approval grants."

No other in-force ADRs are affected. ADR-0008 (facade + handler package) preserved the confirmation seam for this extraction — it's explicitly aligned. ADR-0010 (zone-based file access) is untouched (zone classification and grants are outside the coordinator's scope).
## Context

The agent is a single-user, Telegram-controlled ReAct loop with a sub-agent pool. Today:

- `ConfirmationManager.auto_approve_tools` is a process-lifetime set, cleared only by `/reset`. Grants leak across prompts.
- Sub-agents use `_headless_confirm_bridge` (on `BuiltinExecutor`), which has no approve-all path — every sensitive file op prompts the operator.
- `get_agent_result` blocks on one `agent_id`; there is no "wait for any" primitive. The main agent cannot cancel its own sub-agents.
- `/stop` sets the main agent's `_cancel_event` but does not cancel on-demand/scheduled sub-agents (plan-step/diagnostic are covered by the `PlanExecutor` bridge thread).
- Trace IDs (`r-<8hex>`) correlate logs across a run, but there is no monotonic "Prompt #N" the operator can reference or query.

In-force ADRs constraining this design: ADR-0005 (SubAgentSupervisor owns the sub-agent lifecycle boundary), ADR-0006 (source categories for visibility/capacity), ADR-0007 (AgentRuntime for construction), ADR-0010 (zone-based file access control). None are superseded by this change.

### Component diagram (C4 — container internals)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ Agent Process (single user)                                                 │
│                                                                              │
│  ┌──────────────────────┐    ┌────────────────────────────────────────────┐ │
│  │ TelegramInterface    │    │ AgentController (main, depth 0)             │ │
│  │  - _run_agent_task_  │───▶│  run():                                     │ │
│  │    locked()          │    │   bind_run_context(prompt_id)  ◄ NEW       │ │
│  │  - cmd_stop          │    │   executor._prompt_approval_set = ◄ NEW    │ │
│  │  - cmd_prompts  ◄NEW │    │       confirmation.auto_approve_tools      │ │
│  │  - send_subagent_    │    │   executor._current_prompt_id = ◄ NEW      │ │
│  │    confirmation_     │    │   react_loop(...)                          │ │
│  │    prompt()          │    │   (executor._prompt_registry wired in      │ │
│  │                      │    │    main.py, not per-run)                    │ │
│  └──────────┬───────────┘    │   finally:                                  │ │
│             │                │     clear_auto_approve()       ◄ NEW       │ │
│             │                │     clear executor per-prompt refs ◄ NEW   │ │
│             │                └────────────────────────────────────────────┘ │
│             │                          │                                    │
│             ▼                          ▼                                    │
│  ┌──────────────────────┐    ┌────────────────────────────────────────────┐ │
│  │ telegram_callbacks   │    │ BuiltinExecutor (shared, process-lifetime) │ │
│  │  - cb_subagent_      │    │  _prompt_approval_set: set[str] | None ◄NEW│ │
│  │    confirm()         │    │  _current_prompt_id: int | None      ◄NEW │ │
│  │    + subconfirm_all  │    │  _prompt_registry: PromptRegistry    ◄NEW  │ │
│  │      ◄ NEW           │    │  _headless_confirm_bridge():               │ │
│  └──────────────────────┘    │    if tool in _prompt_approval_set: ◄NEW  │ │
│                              │      auto-confirm (no prompt)               │ │
│                              │    else: prompt + "Approve all" button     │ │
│                              │  _run_table: + wait_for_any_agent ◄NEW      │ │
│                              │             + cancel_agent         ◄NEW     │ │
│                              └────────────────────────────────────────────┘ │
│                                              │                              │
│              ┌───────────────────────────────┘                              │
│              ▼                                                              │
│  ┌──────────────────────┐    ┌────────────────────────────────────────────┐ │
│  │ SubAgentSupervisor   │    │ SubAgentRegistry (singleton)                │ │
│  │  submit()            │───▶│  cancel(agent_id) / cancel_all_managed()    │ │
│  │   + record agent_id   │    │  get(agent_id) → SubAgentRecord            │ │
│  │     against prompt ◄NEW│   │  _result_event per record                  │ │
│  └──────────┬───────────┘    └────────────────────────────────────────────┘ │
│             │                          ▲                                    │
│             ▼                          │                                    │
│  ┌──────────────────────┐    ┌────────────────────────────────────────────┐ │
│  │ ThreadPoolExecutor    │    │ PromptRegistry (singleton) ◄ NEW           │ │
│  │  (sub-agent pool)     │    │  start(trace_id, text) → PromptRecord #N    │ │
│  │  A, B, C run here     │    │  finish(prompt_id, status)                 │ │
│  └──────────────────────┘    │  add_sub_agent(prompt_id, agent_id)         │ │
│                              │  persist → data/prompts.jsonl               │ │
│                              └────────────────────────────────────────────┘ │
│                                                                              │
│  ┌──────────────────────┐    ┌────────────────────────────────────────────┐ │
│  │ agent_logging        │    │ log_query built-in tool                     │ │
│  │  bind_run_context(): │    │  + prompt_id filter param ◄ NEW             │ │
│  │    + prompt_id ◄ NEW │    │  (owned by runtime-log-introspection spec)  │ │
│  └──────────────────────┘    └────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────┘
```

**Boundaries & responsibilities:**
- `BuiltinExecutor` is the single shared instance bridging main and sub-agent confirmation paths. New per-prompt fields (`_prompt_approval_set`, `_current_prompt_id`) are set at `run()` start, cleared at `run()` end. `_prompt_registry` is a long-lived reference wired once in `main.py` (not per-run) — the registry is a process singleton.
- `PromptRegistry` is a new process-singleton, persisted to `data/prompts.jsonl`. It does not touch the ReAct loop or confirmation flow; it is observed by the supervisor and queried by `/prompts` and `log_query`.
- `SubAgentSupervisor` gains one new call: `PromptRegistry.add_sub_agent(prompt_id, agent_id)` after `register_run`. No change to its threading model or lifecycle ownership (ADR-0005 preserved).
- `agent_logging` gains one new bound field (`prompt_id`) in `bind_run_context()`. No change to the LogEvent taxonomy or dual-sink layout (ADR-0004 preserved).

## Goals / Non-Goals

**Goals:**
- Approval grants expire when the prompt's results are presented (per-prompt TTL).
- One "Approve all `<tool>`" covers the main agent and all its sub-agents for that prompt.
- Main agent can consume sub-agent results in completion order (council pattern) and cancel sub-agents it no longer needs.
- `/stop` cancels all sub-agents (on-demand, scheduled, plan-step, diagnostic).
- Operator can reference "Prompt #N" and filter logs by it.

**Non-Goals:**
- Main-agent detachment (returning to idle while sub-agents run) — explicitly out of scope. The main agent still blocks; the next message is deferred.
- Blanket "Approve all file ops" button — per-tool only. Can be added later if chatty cases emerge.
- Confirmation-gated `cancel_agent` — the LLM cancels its own workers freely; operator retains `/agents cancel` as override.
- Multi-user support — single-user tool.
- Changing the sub-agent depth cap (stays at 1).
- Changing the zone-based file access control model (ADR-0010 preserved). The approve-all check short-circuits the zone-triggered *confirmation* (auto-satisfies it via `confirm(token)`), not the zone *classification* itself — `execute()` still runs zone classification first and stages out-of-zone/agent-internal ops as `requires_confirmation` (`react_loop.py:1275-1288`), then the approve-all check at line 1282 auto-satisfies that confirmation. **Behavior expansion to acknowledge:** today sub-agents have no approve-all path, so every agent-internal/UNRECOGNISED file op prompts the operator. The shared `_prompt_approval_set` (D2) means a single "Approve all `file_write`" now lets any sub-agent write to agent-internal/UNRECOGNISED zones without a new prompt for the rest of the prompt. This is intended (council pattern) but must be pinned down in a spec scenario so the boundary is explicit, not implicit.

## Decisions

### D1: Clear `auto_approve_tools` in `run()` finally, not per-run `ConfirmationManager`

**Choice:** Add `self._confirmation.clear_auto_approve()` to `AgentController.run()`'s finally block (`agent_controller.py:181`).

**Why over alternatives:** Constructing a fresh `ConfirmationManager` per `run()` would work but changes construction wiring broadly (the manager is referenced by the executor, the Telegram interface, and tests). Clearing the existing set in `finally` is a one-line change with the same semantic effect and no construction-side ripple. `/reset` still calls `clear_auto_approve()` too — it's now redundant for the approval set but remains the only way to clear working memory.

**Alternative considered:** Per-run `ConfirmationManager` constructed in `run()`. Rejected — larger blast radius, breaks the shared-instance assumption in `BuiltinExecutor._headless_confirm_bridge` and `telegram_callbacks.cb_subagent_confirm`.

### D2: Shared approval set via `_prompt_approval_set` on `BuiltinExecutor`

**Choice:** Add `_prompt_approval_set: Optional[set[str]]` to `BuiltinExecutor`. At `run()` start, set it to `self._confirmation.auto_approve_tools` (the same set object). At `run()` end, set it to `None`. `_headless_confirm_bridge` checks `if tool_name in self._prompt_approval_set` before prompting.

**Why:** `BuiltinExecutor` is a single shared instance (constructed in `main.py`, used by both main and sub-agent paths). Setting the field to reference the main agent's `auto_approve_tools` set means both paths check the same set — one "Approve all file_read" covers everyone. Setting it to `None` at `run()` end means sub-agents that somehow outlive the run (edge case: orphaned sub-agent) re-prompt rather than silently auto-approving.

**Alternative considered:** Pass the approval set through `SupervisionOptions` per submission. Rejected — the set is per-prompt, not per-submission, and threading it through every `submit()` call duplicates the wiring. The executor field is set once per `run()` and read by every sub-agent.

### D3: Active-prompt context via `_current_prompt_id` on `BuiltinExecutor` + `_prompt_registry` wired in `main.py`

**Choice:** Add `_current_prompt_id: Optional[int]` to `BuiltinExecutor`, set at `run()` start, cleared at `run()` end. Add `_prompt_registry: Optional[PromptRegistry]` to `BuiltinExecutor`, wired once in `main.py` (long-lived, not per-run). `AgentTools._exec_spawn_agent` (`builtin_tools/agents.py`) reads `owner._prompt_registry` and `owner._current_prompt_id` and calls `add_sub_agent(prompt_id, agent_id)` immediately after spawning. Recording is intentionally scoped to the model-facing spawn path — plan-step and diagnostic agents launched via other paths during an active prompt are excluded by design.

**Why:** Mirrors the `_prompt_approval_set` pattern (D2) for the per-run piece. Single shared instance, no thread-local, no new params on `spawn_agent`. The supervisor already has a back-reference to the executor (via the `AgentTools` owner chain), so it can read both fields at submit time. The registry reference is long-lived because the registry is a process singleton — wiring it per-run would be redundant.

**Alternative considered:** Thread-local. Rejected — the codebase explicitly avoids process-global mutable state for correctness-critical behavior (`trace_context.py` comment).

### D4: `wait_for_any_agent` via 200ms poll loop

**Choice:** `_exec_wait_for_any_agent` loops with `time.sleep(0.2)`, checking `record._result_event.is_set()` and `record.status in ("done", "failed", "cancelled")` for each candidate. Returns the first completed. Timeout returns `{status: "timeout"}`.

**Why:** Zero new threading primitives. Mirrors how `get_agent_result` works (it also blocks on a single `_result_event.wait(timeout)`). 200ms latency is fine for a model-facing tool — the LLM isn't waiting at sub-millisecond granularity. Already-finished agents return immediately on the first iteration.

**Alternative considered:** Shared `threading.Condition` on `SubAgentRegistry`, notified when any record completes. Cleaner wakeup but requires refactoring `SubAgentRecord` to notify the registry on completion — touches the supervisor's terminal path (`sub_agent_supervisor.py:208,238,272`), which is the highest-risk code in the system. Rejected for blast radius.

### D5: `cancel_agent` not confirmation-gated

**Choice:** `_exec_cancel_agent` wraps `registry.cancel(agent_id)` or filters on-demand agents by `agent_id in ("managed","all")` directly. No operator prompt. Narrowing: `cancel_all_managed()` covers scheduled agents too, but `_exec_cancel_agent` intentionally restricts to on-demand agents only — the LLM must not cancel operator-owned scheduled jobs.

**Why:** The LLM cancelling its own spawned workers is analogous to the existing `get_agent_result` timeout-cancel (`builtin_tools/agents.py:291-294`), which is also not confirmed. The operator retains `/agents cancel` and `/stop` as overrides. Adding confirmation would defeat the "agent decides it has enough" autonomy that `wait_for_any_agent` enables.

**Alternative considered:** Confirmation-gated `cancel_agent`. Rejected — defeats the council pattern.

### D6: `/stop` cascade via `cancel_all_managed()`

**Choice:** `cmd_stop` calls `get_registry().cancel_all_managed()` after `iface.agent.cancel()`. This covers on-demand and scheduled sub-agents (`CAPACITY_COUNTED_SOURCES`). Plan-step and diagnostic are already covered by the `PlanExecutor.execute` bridge thread (`execution_plan.py:660-676`).

**Why:** `cancel_all_managed()` already exists for `/agents cancel managed` (`sub_agent_registry.py:159`). One call, no new primitive. The existing `SubAgentRecord.cancel()` interrupts in-flight LLM requests via HTTP transport close (`sub_agent_registry.py:63`), so cancellation is fast.

**Alternative considered:** A new `/stop all` command keeping `/stop` as main-agent-only. Rejected — `/stop` cascading is what users intuitively expect; the current behavior (sub-agents keep running after `/stop`) is a footgun.

### D7: `prompt_id` as first-class log field via structlog bind

**Choice:** `bind_run_context()` in `agent_logging.py` accepts `prompt_id` and binds it into the structlog context, alongside the existing `trace`/`agent`/run-label. `AgentController.run()` calls `bind_run_context(prompt_id=...)` at start. Sub-agents inherit via the existing context propagation (the supervisor's pool threads call `bind_run_context` with the parent's `prompt_id`).

**Why:** The user chose first-class log field over a join on `trace_id`. Binding via structlog context is how `trace` and `agent` already reach every log line — adding `prompt_id` is one new parameter to the existing `bind_run_context` call, no change to the LogEvent taxonomy or dual-sink layout. `log_query` gains a `prompt_id` filter that reads the field directly from `agent.jsonl` — no join needed.

**Alternative considered:** Join on `trace_id` (registry maps `prompt_id → trace_id`, `log_query` filters by trace). Rejected by user choice — first-class field is cleaner for querying.

### D8: `PromptRegistry` persistence to `data/prompts.jsonl`

**Choice:** Append-only JSONL, one line per prompt. `start()` writes a line with `prompt_id`, `trace_id`, `text`, `started_at`, `status="running"`, `sub_agent_ids=[]`. `add_sub_agent()` updates the in-memory record and rewrites the prompt's line (or appends an update line — see below). `finish()` appends a line with `ended_at` and final `status` and the full `sub_agent_ids` list. On startup, the registry reloads from `data/prompts.jsonl`: replays the log to rebuild the in-memory dict and recover the next `prompt_id` (max existing + 1), so "Prompt #N" is stable across restarts.

**Why:** JSONL is the existing pattern (`agent.jsonl` is JSONL). The frozen proposal says the registry "maps prompt_id to trace_id and sub-agent IDs" — so `sub_agent_ids` must be persisted, not in-memory only. Append-only survives crashes — a prompt that started but never finished is still queryable. Startup reload makes the operator's "Prompt #N" reference stable across restarts (resolves the open question in favor of reload).

**Line format choice:** Append an update line on each `add_sub_agent` (simpler, crash-safe) rather than rewriting the original line (race-prone). On reload, replay: the last line for a given `prompt_id` wins for mutable fields (`status`, `sub_agent_ids`, `ended_at`).

## Risks / Trade-offs

- **[Orphaned sub-agent after `run()` ends]** → If the LLM emits `finish` without calling `get_agent_result` on all spawned sub-agents, the sub-agents keep running. At `run()` end, `_prompt_approval_set` is set to `None`, so their next sensitive file op re-prompts (fail-closed). The prompt currently says "Always follow spawn_agent with get_agent_result" (`prompt_builder.py:143`). Mitigation: the prompt guidance stays; `/stop` cascade is the operator's escape hatch.

- **[Approval set shared across sub-agents]** → One "Approve all file_write" grants all sub-agents write access for the prompt. This is the intended behavior (council pattern), but it means a misbehaving sub-agent can write unexpected files without further prompts. Mitigation: per-tool granularity (not blanket); operator can deny the initial prompt; `/stop` cancels everything.

- **[`prompt_id` not in logs for sub-agents]** → Sub-agents run on pool threads. `bind_run_context` must be called on the pool thread, not just the main thread. The supervisor's `_run_and_notify` (`sub_agent_supervisor.py:147`) runs on the pool thread — it must call `bind_run_context(prompt_id=...)` before `runner.run(task)`. Risk: if forgotten, sub-agent logs lack `prompt_id`. Mitigation: the supervisor already propagates `trace_id`; adding `prompt_id` follows the same path.

- **[`wait_for_any_agent` poll loop CPU]** → 200ms sleep loop is negligible CPU. But if the LLM calls it with a 300s timeout and no agents finish, it sleeps 1500 times. Mitigation: acceptable — same order as `get_agent_result`'s `event.wait(timeout=300)`.

- **[`data/prompts.jsonl` unbounded growth]** → Append-only file grows forever. Mitigation: same as `agent.jsonl` (daily gzip rotation, 30 backups). Apply the same rotation policy or document it as out of scope for this change.

## Migration Plan

- **No data migration.** `data/prompts.jsonl` is new; if absent, the registry starts fresh at prompt_id=1.
- **No config change.** No new config keys.
- **Behavior change: grants expire per-prompt.** Operators who relied on "Approve all" persisting across prompts will see re-prompts. This is the intended fix; document in the changelog.
- **Behavior change: `/stop` cancels sub-agents.** Operators who relied on sub-agents surviving `/stop` will see them cancelled. This is the intended fix; document in the changelog.
- **Rollback:** Revert the change. `auto_approve_tools` returns to process-lifetime; `/stop` returns to main-agent-only. No data to roll back.

## Open Questions

- **`data/prompts.jsonl` rotation:** Should it get the same daily gzip rotation as `agent.jsonl`, or is it small enough to leave unbounded for now? Lean toward leaving it unbounded for this change (prompts are infrequent) and adding rotation later if needed.

(Resolved: startup reload of `prompt_id` sequence — decided in D8 in favor of reload so "Prompt #N" is stable across restarts.)
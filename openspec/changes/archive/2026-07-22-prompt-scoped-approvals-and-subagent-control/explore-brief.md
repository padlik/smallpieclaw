# Explore Brief: prompt-scoped-approvals-and-subagent-control

## Context

Single-user Telegram-controlled agent. Main agent runs one prompt at a time (per-user asyncio lock serializes; second message deferred). Main agent can spawn sub-agents that run in parallel on a ThreadPoolExecutor; main agent blocks on `get_agent_result` to collect. Sub-agents cannot spawn further sub-agents (depth cap = 1, already enforced).

## Problem

1. **Approval TTL is wrong.** `auto_approve_tools` lives on `ConfirmationManager` (process-lifetime). `AgentController.run()` does NOT clear it. "Approve all file_read" granted during Prompt #1 silently carries into Prompt #2, #3, ... until `/reset` or process restart. User wants grants to expire when the prompt's results are presented.

2. **Sub-agents have no approve-all.** Sub-agents use `_headless_confirm_bridge` (separate from main agent's `ConfirmationManager.auto_approve_tools`). Every sensitive `file_read`/`file_write`/`file_patch` by a sub-agent prompts the operator individually. No "Approve all for this prompt" path. With 3 sub-agents × 5 file ops = 15 prompts.

3. **No council-style consumption.** `get_agent_result` blocks on one specific `agent_id`. No "wait for whichever finishes first" primitive. LLM tends to call in spawn order; fast finishers don't jump the queue.

4. **No way for main agent to cancel sub-agents it no longer needs.** Only operator via `/agents cancel` can stop sub-agents. Main agent has no tool.

5. **`/stop` doesn't cascade to on-demand sub-agents.** `/stop` sets main agent's `_cancel_event` but on-demand/scheduled sub-agents have their own events. Plan-step/diagnostic sub-agents ARE cancelled (via `PlanExecutor.execute` bridge thread, `execution_plan.py:660-676`), but on-demand/scheduled are not. User's "things are gone wrong" intuition is violated.

6. **No prompt ID for log tracking.** Trace IDs (`r-<8hex>`) exist per run and propagate to sub-agents, but there's no monotonic "Prompt #N" the operator can reference, and no registry mapping prompt → trace + sub-agent IDs + status.

## Alternatives Rejected

- **Per-run `ConfirmationManager` (constructed in `run()`)**: Would work but changes construction wiring broadly. Rejected in favor of clearing the existing set in `run()`'s finally block — smaller blast radius.
- **Blanket "Approve all file ops" button**: Less clicking but loses granularity (file_read vs file_write vs file_patch are structurally different). Rejected in favor of per-tool buttons, matching the existing main-agent UI. Blanket can be added later if chatty cases emerge.
- **Confirmation-gated `cancel_agent`**: Safer but defeats the "agent decides it has enough" autonomy. Rejected — `cancel_agent` is the LLM cleaning up its own spawned workers, analogous to existing `get_agent_result` timeout-cancel (not confirmed). Operator retains `/agents cancel` as override.
- **Main-agent detachment (return to idle while sub-agents run)**: User's original sketch had this, but user explicitly approved the current "main agent blocks, next message deferred" behavior. Out of scope for this change.

## Final Approach — Labels, Dimensions, Mapping Tables

### Approval scope dimensions

| Dimension | Value | Evidence |
|---|---|---|
| Tool name | Exact string (e.g. "file_read"); no wildcards | `auto_approve_tools.add(tool_name)` |
| Path | All paths; check fires before path/zone re-eval | `react_loop.py:1282` short-circuits |
| Lifetime | Per-prompt: cleared at `run()` end | NEW: `clear_auto_approve()` in `run()` finally |
| Agent instance | Shared set: main + all sub-agents for this prompt | NEW: `_prompt_approval_set` on `BuiltinExecutor` |
| Other tools | Untouched; `shell` always confirmed/blocked | `builtin_executor.py:424` |

### Sub-agent source categories (existing, from `sub_agent_registry.py`)

| Source | Cancelled by `/stop` today? | Cancelled by enhanced `/stop`? |
|---|---|---|
| `on-demand` (spawn_agent) | No | Yes (via `cancel_all_managed`) |
| `scheduled` | No | Yes (via `cancel_all_managed`) |
| `plan-step` | Yes (bridge thread) | Yes (unchanged) |
| `diagnostic` | Yes (bridge thread) | Yes (unchanged) |

`CAPACITY_COUNTED_SOURCES = frozenset({SOURCE_ON_DEMAND, SOURCE_SCHEDULED})` — what `cancel_all_managed()` targets.

### New tools

| Tool | Args | Returns | Confirmation? |
|---|---|---|---|
| `wait_for_any_agent` | `agent_ids: list[str]`, `timeout: int` | First completed agent's `{agent_id, result, status}` or `{status: "timeout"}` | No |
| `cancel_agent` | `agent_id: str` (or "managed"/"all") | `{success, output}` | No (LLM cancels own workers) |

### Prompt registry fields

| Field | Type | Notes |
|---|---|---|
| `prompt_id` | int | Monotonic, starts at 1 |
| `trace_id` | str | `r-<8hex>`, the existing trace |
| `text` | str | First 200 chars of user message |
| `started_at` | float | `time.time()` |
| `ended_at` | Optional[float] | None while running |
| `status` | str | "running" \| "done" \| "failed" \| "cancelled" |
| `sub_agent_ids` | list[str] | Appended as sub-agents spawn |

Persisted to `data/prompts.jsonl` (append-only, one JSON line per prompt).

## Cross-Module Data Flows

### Prompt tracking
```
TelegramInterface._run_agent_task_locked (telegram_interface.py:441)
  → PromptRegistry.start(trace_id, text) → PromptRecord #N
  → loop.run_in_executor(agent_handler → AgentController.run)
      → AgentRuntime.build_react_context → react_loop
          → spawn_agent → SubAgentSupervisor.submit
              → PromptRegistry.add_sub_agent(#N, agent_id)
  → finally: PromptRegistry.finish(#N, status)
```

### Per-prompt approval TTL
```
AgentController.run (agent_controller.py:147)
  → self._builtin_executor._prompt_approval_set = self._confirmation.auto_approve_tools
  → react_loop
      → main agent file_read sensitive → request_confirmation
          → if tool_name in auto_approve_tools: auto-confirm (react_loop.py:1282)
      → sub-agent file_read sensitive → _headless_confirm_bridge (builtin_executor.py:453)
          → if tool_name in self._prompt_approval_set: auto-confirm (NEW)
          → else: Telegram prompt with "Approve all {tool}" button (NEW)
              → operator taps → cb_subagent_confirm → auto_approve_tools.add(tool_name)
  → finally: clear_auto_approve() + _prompt_approval_set = None
```

### `/stop` cascade
```
cmd_stop (telegram_commands.py:247)
  → iface.agent.cancel()  # main agent _cancel_event
  → get_registry().cancel_all_managed()  # on-demand + scheduled (NEW)
  → PlanExecutor bridge thread notices main cancel → plan cancel_event set → plan-step/diagnostic cancelled (existing)
```

### `wait_for_any_agent` / `cancel_agent`
```
react_loop → LLM emits tool action
  → builtin_executor.execute("wait_for_any_agent", args)
      → AgentTools._exec_wait_for_any_agent → polls registry for first done
  → builtin_executor.execute("cancel_agent", args)
      → AgentTools._exec_cancel_agent → registry.cancel(agent_id) or cancel_all_managed()
```

## Resolved Questions

1. **PromptRegistry active-prompt context for supervisor**: **Field on `BuiltinExecutor`.** Add `_current_prompt_id`, set at `run()` start, cleared at `run()` end. Supervisor reads it via its owner reference. Mirrors the `_prompt_approval_set` pattern — single shared instance, no thread-local magic, no new params on `spawn_agent`.

2. **`wait_for_any_agent` polling vs. condition**: **Poll loop.** 200ms sleep-loop checking `_result_event.is_set()` on each candidate. Zero new threading primitives, ~30 lines, mirrors how `get_agent_result` works. Latency is fine for a model-facing tool.

3. **`/prompts` command scope**: **List-only.** List recent N (default 20) with ID, status, elapsed, sub-agent count. Detail view can come later.

4. **`log_query` prompt_id filter**: **First-class log field.** Add `prompt_id` as a field in every log line (not a join on trace_id). Requires changing `agent_logging.py` to thread the active prompt_id through every log call. Bigger change but cleaner querying.

5. **prompt_id propagation to logging**: **Bind via structlog context.** Add `prompt_id` to `bind_run_context()` at `run()` start. Already how trace/agent/run-label are bound. Minimal new code — just add `prompt_id` to the bind call. Sub-agents inherit via the existing context propagation.

6. **Sub-agent "Approve all" button callback routing**: `cb_subagent_confirm` currently handles `subconfirm_yes` / `subconfirm_no`. Adding `subconfirm_all:{token}:{tool_name}` extends the callback data format. Precedent exists (main-agent `confirm_all` uses the same format).
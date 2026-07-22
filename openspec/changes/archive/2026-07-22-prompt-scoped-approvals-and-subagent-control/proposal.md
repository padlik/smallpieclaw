## Why

The agent's operator-approval model and sub-agent control surface are misaligned with how the system is actually used. Three concrete problems:

1. **Approval grants outlive the prompt that needed them.** `auto_approve_tools` lives on `ConfirmationManager` for the process lifetime and is cleared only by `/reset`. An "Approve all file_read" granted during Prompt #1 silently carries into Prompt #2, #3, and beyond — a permission-isolation hazard the operator cannot see. The operator's mental model is "I approved this for the current task," not "I approved this forever."

2. **Sub-agents have no approve-all path.** Sub-agents use a separate confirmation bridge (`_headless_confirm_bridge`) with no access to `auto_approve_tools`. Every sensitive `file_read`/`file_write`/`file_patch` by every sub-agent prompts the operator individually. With 3 sub-agents × 5 file ops, that is 15 prompts for one task — unusable.

3. **The main agent cannot manage its own sub-agents.** `get_agent_result` blocks on one specific `agent_id`; there is no "wait for whichever finishes first" primitive, so fast-finishers sit idle while the LLM waits in spawn order. There is no tool for the main agent to cancel a sub-agent it no longer needs — only the operator can, via `/agents cancel`. And `/stop` does not cascade to on-demand/scheduled sub-agents, so "stop everything" actually means "stop the main agent and hope the sub-agents finish on their own."

A secondary gap: there is no stable, human-friendly prompt identifier for log correlation. Trace IDs (`r-<8hex>`) exist but are not surfaced as a monotonic "Prompt #N" the operator can reference or query.

## What Changes

- **Per-prompt approval TTL**: `auto_approve_tools` is cleared at the end of `AgentController.run()`. Grants expire when the prompt's results are presented. `/reset` still works but is no longer the only clear path.
- **Shared approval set for sub-agents**: Sub-agents check the main agent's `auto_approve_tools` via a shared reference on `BuiltinExecutor`. One "Approve all file_read" covers the main agent and all its sub-agents for that prompt.
- **"Approve all" button on sub-agent prompts**: Sub-agent confirmation prompts gain a per-tool "Approve all `<tool>`" button, mirroring the main-agent UI. Per-tool, not blanket — `file_read`, `file_write`, `file_patch` are granted separately.
- **`wait_for_any_agent` tool**: Returns the first completed sub-agent's result from a set of agent IDs. The LLM calls it in a loop to collect results in completion order (council pattern), deciding after each whether it has enough.
- **`cancel_agent` tool**: Lets the main agent cancel a sub-agent it no longer needs. Wraps the existing `registry.cancel()` / `cancel_all_managed()`. Not confirmation-gated — the LLM cancelling its own spawned workers is analogous to the existing `get_agent_result` timeout-cancel.
- **`/stop` cascade**: `/stop` now also calls `cancel_all_managed()`, cancelling on-demand and scheduled sub-agents. Plan-step and diagnostic sub-agents were already covered by the existing `PlanExecutor` bridge thread. `shell` is never auto-approved — it remains always-confirmed for the main agent and always-blocked for sub-agents.
- **Non-goal**: Main-agent detachment (returning to idle while sub-agents keep running) is explicitly out of scope. The main agent still blocks for the duration of its run; the next user message is deferred. `wait_for_any_agent` and `cancel_agent` give the agent control over *which* sub-agents it waits on, not freedom to stop waiting.
- **Prompt registry**: A `PromptRegistry` assigns a monotonic "Prompt #N" at task entry, persists to `data/prompts.jsonl`, and tracks `{prompt_id, trace_id, text, started_at, ended_at, status, sub_agent_ids}`. A new `/prompts` command lists recent prompts.
- **`prompt_id` as a first-class log field**: `prompt_id` is bound into the structlog context at `run()` start via `bind_run_context()` (alongside the existing `trace`/`agent`/run-label). Every log line carries it. `log_query` gains a `prompt_id` filter.

## Capabilities

### New Capabilities
- `prompt-tracking`: Monotonic prompt ID assignment, persistence, and `/prompts` command for listing recent prompts. The registry maps prompt_id to trace_id and sub-agent IDs.
- `sub-agent-council-control`: `wait_for_any_agent` and `cancel_agent` tools giving the main agent council-style consumption and cancellation of its sub-agents.

### Modified Capabilities
- `sub-agent-supervision`: Sub-agents now share the main agent's per-prompt approval set. Sub-agent confirmation prompts gain a per-tool "Approve all" button. Supervisor records the spawned agent_id against the active prompt in the PromptRegistry.
- `telegram-command-surface`: `/stop` cascades to on-demand and scheduled sub-agents via `cancel_all_managed()`. New `/prompts` command lists recent prompts.
- `structured-event-logging`: `prompt_id` is bound into structlog context at `run()` start and appears in every log line.
- `runtime-log-introspection`: `log_query` gains a `prompt_id` filter parameter.
- `builtin-tool-execution`: `auto_approve_tools` is cleared at `run()` end (per-prompt TTL). `_headless_confirm_bridge` checks the shared approval set before prompting. New `wait_for_any_agent` and `cancel_agent` tools are registered.

## Impact

- **`agent_controller.py`**: `run()` finally block clears `auto_approve_tools` and unsets `_prompt_approval_set`/`_current_prompt_id` on the executor. `run()` start binds `prompt_id` into structlog context and sets the shared references.
- **`builtin_executor.py`**: New `_prompt_approval_set`, `_current_prompt_id`, and `_prompt_registry` fields. `_headless_confirm_bridge` checks the set before prompting. New tool routing entries for `wait_for_any_agent` and `cancel_agent`.
- **`builtin_tools/agents.py`**: New `_exec_wait_for_any_agent` and `_exec_cancel_agent` handlers.
- **`builtin_tools/descriptors.py` + `schemas.py`**: New tool descriptors and schemas for the two new tools.
- **`confirmation.py`**: No structural change — `clear_auto_approve()` already exists; it just gets called from a new site.
- **`sub_agent_supervisor.py`**: `submit()` records the spawned `agent_id` against the active prompt via the registry reference on the executor.
- **`telegram_interface.py`**: `send_subagent_confirmation_prompt` gains the "Approve all" button. `_run_agent_task_locked` calls `PromptRegistry.start()`/`finish()`.
- **`telegram_callbacks.py`**: `cb_subagent_confirm` handles the new `subconfirm_all` callback.
- **`telegram_commands.py`**: `cmd_stop` calls `cancel_all_managed()`. New `cmd_prompts`.
- **`agent_logging.py`**: `bind_run_context()` accepts and binds `prompt_id`.
- **`builtin_tools/secrets_log.py`**: `log_query` gains `prompt_id` filter parameter.
- **`prompt_registry.py`** (new): `PromptRegistry` + `PromptRecord` dataclass, persistence to `data/prompts.jsonl`.
- **`prompt_builder.py`**: Prompt guidance for `wait_for_any_agent`, `cancel_agent`, and the per-prompt approval model.
- **Tests**: New tests for prompt registry, per-prompt approval TTL, sub-agent approve-all, `wait_for_any_agent`, `cancel_agent`, `/stop` cascade, `/prompts` command, `prompt_id` log field, `log_query` prompt_id filter.
## Why

The react loop's blocking step gate—which pauses the agent and prompts the operator for an extension—silently kills jobs whenever the operator cannot attend their phone. Three separate caps feed this gate (`max_iterations` for the main agent, `scheduled_max_iterations` for background agents, `plan_max_iterations` for plan-step agents), each with the same failure mode. Legitimate long-running jobs (variable-length record batches, multi-step browser automation) are cut at arbitrary step counts rather than running to completion. Separately, agents stuck in failure loops—repeatedly invoking the same tool with the same error—have no self-termination mechanism and burn budget indefinitely. Doom-loop detection replaces all three caps as the universal runtime bound.

## What Changes

- **Universal gate removal**: the blocking step gate is removed from the react loop. All three step-count caps (`max_iterations`, `scheduled_max_iterations`, `plan_max_iterations`) are silently deprecated at runtime — config fields and `scheduler.toml` entries are preserved for backward compatibility but have no effect on execution.
- **`schedule` tool parameter removed**: the `max_iterations` parameter is removed from the `schedule` built-in tool (add-action) descriptor and schema. Existing `scheduler.toml` files with `max_iterations` entries are read but silently ignored.
- **New `step_notify_interval` field** (default: 30, type: int): the main agent sends a fire-and-forget Telegram notification at each multiple of this step count without blocking. Setting `0` disables notifications. Scheduled and plan-step sub-agents do not receive milestone notifications.
- **New doom-loop detection**: three identical consecutive `(tool_name, error[:200])` pairs cause any agent to self-terminate with a descriptive stop message. Any successful tool call resets the counter.
- `RunPanel` gains a milestone notification method (no buttons, non-blocking).
- `_handle_extend_progress`, `_send_extend_prompt`, `__EXTEND__:` signal handling, and extend-button callbacks in `telegram_callbacks.py` are retired as dead code.
- Step display readouts that showed `Step: X/max_iterations` are updated to remove the now-meaningless cap denominator.
- README and relevant documentation updated to reflect the removed Extend prompt and new behavior.

**Risk acknowledgment**: universal gate removal means unattended scheduled and plan-step agents have no hard step cap. Doom-loop detection mitigates exact-repeat failures but does not backstop slow-progress or varied-error loops. This budget risk is accepted.

## Capabilities

### New Capabilities

- `step-milestone-notifications`: at each multiple of `step_notify_interval` steps, the main agent sends a Telegram notification (fire-and-forget, no buttons, triggers phone notification). Value `0` disables. Scheduled and plan-step sub-agents never emit milestone notifications.
- `doom-loop-detection`: when the same tool call fails with the same error three consecutive times (exact match on `tool_name + error[:200]`), any agent self-terminates with a descriptive stop message. Any successful tool call resets the counter.

### Modified Capabilities

(none — step-extension behavior was not previously specified)

## Impact

| File | Change |
|---|---|
| `config_schema.py` | Add `step_notify_interval: int = 30`; update docstrings on `max_iterations`, `scheduled_max_iterations`, `plan_max_iterations` marking them as deprecated no-ops |
| `react_loop.py` | Remove step-gate loop condition + `request_extension()` call; add milestone notify call; add doom-loop fields to `_LoopState` and detection logic after each tool result |
| `agent_controller.py` | Wire `milestone_notify_fn = run_panel.milestone_notify` into `ReactContext`; update step display readout |
| `telegram_interface.py` | Add `RunPanel.milestone_notify(step: int)` fire-and-forget method; retire `_handle_extend_progress()` and `_send_extend_prompt()` |
| `telegram_callbacks.py` | Remove extend-button callback handlers |
| `telegram_commands.py` | Update `cmd_status()` to show `step_notify_interval`; fix step display; update `cmd_help()` |
| `sub_agent_supervisor.py` | Fix `Completed {iteration}/{max_iterations} iterations` display to remove denominator |
| `builtin_tools/descriptors.py` | Remove `max_iterations` parameter from `schedule` (add-action) descriptor |
| `builtin_tools/schemas.py` | Remove `max_iterations` field from `schedule` (add-action) schema |
| `builtin_tools/schedule.py` | Remove `max_iterations` parameter from tool handler |
| `builtin_tools/agents.py` | Remove `max_iterations` threading through spawn_agent args |
| `scheduler.py` | Continue reading `max_iterations` from job meta for backward compat, but do not pass it to any runtime path |
| `README.md` | Remove Extend buttons description; update `max_iterations` / `scheduled_max_iterations` documentation |

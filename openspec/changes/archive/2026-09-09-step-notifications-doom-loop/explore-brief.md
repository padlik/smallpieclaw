# Explore Brief — step-notifications-doom-loop

## Alternatives Rejected

| Alternative | Rejected Because |
|---|---|
| `/autoapprove on\|off` command — ghost operator approves all confirmations | Shelved; UX unclear and scope too broad |
| `/autoextend` with budget pool (x5/x10 multipliers) | Wrong mental model; operator can't predict multiplier needs |
| Add extend buttons to existing step-limit prompt | Prompt itself is the problem — operator can't attend phone |
| Thread `auto_extend` flag + `on_step_milestone_fn` through `SupervisionOptions` | Unnecessary once gate is removed — all complexity from the blocking event.wait() |
| Keep `max_iterations` as hard limit but increase default | Doesn't solve the unattended phone scenario |

## Final Approach — Complete Mapping

### Config fields

| Field | Old behavior | New behavior |
|---|---|---|
| `max_iterations` | Hard gate: blocks agent, prompts operator | Silently deprecated at runtime; config field kept, no effect |
| `step_notify_interval` | (new) | Notification interval in steps; default 30; 0 = no notifications |

### React loop changes

| What | Old | New |
|---|---|---|
| Loop condition | `while state.step < state.max_steps` | `while True` (no step gate) |
| At milestone step | `request_extension()` → blocks on `event.wait(120)` | Call `ctx.milestone_notify_fn(state.step)` if set (fire-and-forget) |
| Doom-loop fields | (none) | `_last_tool_fail_key: str = ""`, `_tool_fail_repeat: int = 0` in `_LoopState` |
| Doom-loop limit | (none) | `_DOOM_LOOP_LIMIT = 3` — same `(tool_name, error[:200])` key 3× → graceful abort |
| Key reset | (none) | Reset to `""` / 0 on any successful tool call |

### Cross-module data flow (step milestone notification)

```
config_schema.py: step_notify_interval → AgentConfig
agent_controller.py: ReactContext.milestone_notify_fn = run_panel.milestone_notify
react_loop.py: state.step % ctx.step_notify_interval == 0 → ctx.milestone_notify_fn(state.step)
telegram_interface.py: RunPanel.milestone_notify(step) → send fire-and-forget Telegram message
```

### Cross-module data flow (doom-loop)

```
react_loop.py (self-contained):
  after each tool result:
    if failed: key = f"{tool_name}:{error[:200]}"
      if key == state._last_tool_fail_key: state._tool_fail_repeat += 1
      else: reset key and counter to 1
      if state._tool_fail_repeat >= _DOOM_LOOP_LIMIT: return abort message
    if succeeded: state._last_tool_fail_key = ""; state._tool_fail_repeat = 0
```

### Scheduled jobs

No changes needed. Scheduled jobs (sub-agents spawned by `scheduler.py`) inherit:
- Unlimited runtime (gate removed from react_loop universally)
- Doom-loop protection (react_loop change is universal)
- No per-step Telegram notifications (no `milestone_notify_fn` wired for sub-agents — unattended by design)

## Open Questions (resolved)

| Question | Decision |
|---|---|
| What happens to `_handle_extend_progress()` in RunPanel? | Retire it — dead code is bad |
| What happens to `request_extension()` in `confirmation.py`? | Leave the method (other callers may exist); just stop calling it from react_loop |
| Should `max_iterations` log a deprecation warning? | Truly silent — operator preference |
| Should `step_notify_interval` be configurable per-job in `scheduler.toml`? | Not in this change — follow-up if needed |

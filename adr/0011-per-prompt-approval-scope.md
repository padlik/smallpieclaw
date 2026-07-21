# Use per-prompt scope for operator approval grants

## Status

Accepted

## Date

2026-07-21

## Supersedes

None

## Context

Prior to this change, `ConfirmationManager.auto_approve_tools` was a process-lifetime set, cleared only by `/reset` (`AgentController.reset_task`). An "Approve all file_read" granted during Prompt #1 silently carried into Prompt #2, #3, and beyond — a permission-isolation hazard the operator could not see. The operator's mental model is "I approved this for the current task," not "I approved this forever."

Sub-agents used a separate confirmation path (`_headless_confirm_bridge`) with no access to `auto_approve_tools`, so every sensitive file operation by every sub-agent prompted the operator individually — no approve-all path existed for sub-agents.

The approval model needed to (a) scope grants to a single prompt's lifetime, and (b) cover both the main agent and its sub-agents so one grant suffices for a council of sub-agents working on one task.

## Decision

Approval grants (`auto_approve_tools`) SHALL be per-prompt: the set is cleared at the end of `AgentController.run()`, so grants expire when the prompt's results are presented. `/reset` still clears the set but is no longer the only clear path.

Sub-agents SHALL share the main agent's `auto_approve_tools` via a per-prompt reference (`_prompt_approval_set`) on the shared `BuiltinExecutor` instance, set at `run()` start and set to `None` at `run()` end. The `_headless_confirm_bridge` checks this set before prompting; if the tool name is present, the operation is auto-approved without a new prompt.

The approve-all check short-circuits the zone-triggered *confirmation* (auto-satisfies it via `confirm(token)`), not the zone *classification* itself — `execute()` still runs zone classification first (ADR-0010 preserved). A shared "Approve all file_write" lets any sub-agent write to agent-internal/UNRECOGNISED zones without a new prompt for the duration of the prompt; this is the intended council-pattern behavior.

`shell` is never auto-approved — it remains always-confirmed for the main agent and always-blocked for sub-agents, regardless of any approve-all grant. Approve-all buttons are file-tool-only.

## Consequences

- Good, because grants no longer leak across prompts — the operator's mental model matches the system's behavior.
- Good, because one "Approve all file_read" covers the main agent and all its sub-agents for a task, making the council pattern usable (3 sub-agents × 5 file ops = 15 prompts becomes 1 prompt).
- Good, because orphaned sub-agents that outlive the run re-prompt (fail-closed) since `_prompt_approval_set` is set to `None` at run end.
- Bad, because a shared "Approve all file_write" lets any sub-agent write to agent-internal/UNRECOGNISED zones without further prompts for the prompt — a behavior expansion from the pre-change state where sub-agents had no approve-all path. Mitigated by per-tool granularity (not blanket) and operator override via `/stop`.
- Bad, because operators who relied on "Approve all" persisting across prompts will see re-prompts. This is the intended fix.
- Neutral, because `/reset` becomes redundant for clearing the approval set (it still clears working memory, so it remains useful).
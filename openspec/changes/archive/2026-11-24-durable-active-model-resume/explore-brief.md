# Exploration brief

## Agreed outcome
- Remove blocking live LLM Retry/Cancel cards; retain disk-based `/resume` and `/resume N`. Failed/disabled checkpoint writing means no recovery for that failed request.
- `/resume` admits the current active model once, not the checkpoint's historical model; announce a changed model, including that saved context goes to the newly selected provider. Keep that admitted target immutable.
- Preserve saved messages, completed tool results, loop position, trace and attempt counters. Explicit resume grants the existing single manual continuation, not a refreshed automatic budget.

## Complete decision mapping
| Condition | Outcome |
| --- | --- |
| Cancellation | Existing cancellation outcome; no new checkpoint or retry wait |
| Context/permanent failure | Fixed actionable terminal failure; no new checkpoint |
| Retryable failure in Telegram root/resume, checkpoint enabled and saved | Save eligible state, return failure with `/resume` guidance, unwind owned resources |
| Same failure, checkpoint disabled or save unsuccessful | Return failure stating recovery unavailable; no memory-only continuation |
| Retryable headless failure | Existing bounded policy, terminal failure, no new checkpoint |
| Resume with active model | Admit it once, restore saved state, announce model change when different, continue without tool replay |
| Resume without admissible active model | Actionable local failure, no send, preserve checkpoint |
| Resume preparation failure/cancellation | Preserve original snapshot before send |
| Resume target rejects saved input | Normal sanitized failure, no alternate model or whole-task restart |

## Alternatives and boundaries
Rejected: retain blocking cards (duplicate recovery lifecycle); replace them with a new Resume button (extra tokens/concurrency state); require historical model equality (prevents intentional recovery with another selected model); reset attempts or restart task on model switch (weakens boundedness and risks repeated effects). Prompt catalog reductions, scheduled-selector changes, Ollama replacement and `/models` changes are out of scope.

## Data flow and safeguards
Telegram root/resume explicitly supplies checkpoint eligibility to controller/ReactContext; `_handle_llm_error` saves loop/attempt state through CheckpointStore and returns terminal guidance without confirmation tokens/waits. Telegram `/resume` selects an eligible checkpoint; controller's run-client factory snapshots current selection, validates/restores saved state and announces handoff before model I/O; adapters retain existing history conversion. Exact-snapshot retirement occurs immediately before actual send; preparation failures preserve bytes; existing client/lease cleanup and cancellation precedence remain. No new exactly-once guarantee.

## Open questions
No blocking product questions. Planning defaults: reject obsolete timeout key with removal guidance; old card callbacks are inert and direct to `/resume`; replace interactive capability with explicit durable-checkpoint eligibility, default false, enabled only for Telegram root/resume. Abandoned checkpoints remain operator-managed; no new discard command or automatic cleanup.

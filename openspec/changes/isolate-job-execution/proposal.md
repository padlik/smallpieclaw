## Why

Scheduled executions currently have private conversation buffers but can still consume the operator's live context, share approval state, and place results into operator recall. On-demand children can also outlive their parent turn and independently notify Telegram. These ownership leaks cause background work to interfere with the single operator session; the system needs distinct, enforceable execution boundaries.

## What Changes

- Make on-demand **turn-owned children** answer only to their spawning **operator turn**. The operator must consume or cancel outstanding children before its turn closes; their approvals can still be handled in that active turn under the existing path and grant policy. Children do not independently send completion messages or publish results to operator recall.
- Give every **scheduled run**, including cron, once, and `run_now`, **job-owned context** only. It cannot read or change operator memory or inject job results/history into operator context. Runs retain the existing default security boundaries and depth-1 shell restriction, but never consult interactive grants or render approval prompts: deny any additional permission request immediately, regardless of whether the operator is active. No per-job capabilities or detached operator jobs are introduced.
- Change the existing truncated execution log so each **job execution record** retains its complete result within the existing bounded history; expired details are reported as unavailable, not recreated from operator context. Preserve `notify=false` suppression for both successful and failed run-result notices while retaining existing launch-failure alerts. When `notify=true`, both success and failure result notices wait until the operator turn and typing finish, then arrive silently: an expandable quote for results that fit or a compact message with access to retained details for longer results. Failure notices retain the error type and next run time. This intentionally replaces today's immediate plain-text scheduled notices with deferred, collapsed presentation; viewing results does not become a new operator prompt.
- Preserve existing `/jobs`, `/agents`, `/stop`, schedule-tool actions, source visibility/capacity policy, and `skip`/`parallel` overlap options. Add authenticated, read-only `/jobs history <tag>` (retained run IDs and statuses, including removed once-jobs) and `/jobs result <execution-id>` (one retained complete outcome); neither invokes the operator agent or injects context. Keep the single-authorised-operator deployment assumption; multi-user session coordination and pairing changes are out of scope.

## Capabilities

### New Capabilities

- `scheduled-job-execution`: Job-owned runtime context and outcomes, noninteractive permission boundary, and quiet Telegram result presentation.

### Modified Capabilities

- `sub-agent-supervision`: Bind on-demand children and their results to the active operator turn; close the turn only after child completion or cancellation, without independent child notifications. Carve scheduled sub-agents out of the older shared-approval-set and confirmation-prompt requirements.
- `sub-agent-context`: Apply automatic parent context only to on-demand spawns, not scheduler-owned launches; exclude operator context and memory from scheduled executions.
- `approval-grants`: Keep interactive child approvals scoped through the depth-0 operator's existing ledger while preventing scheduled executions from consulting or creating interactive grants.
- `llm-error-recovery`: Preserve scheduled failure classification and error/next-run content; when job run-result notifications are enabled, defer the failure notice until the operator turn is idle and send it without sound.
- `telegram-command-surface`: Add read-only history/result views under `/jobs` while preserving all existing job and agent management commands.

## Impact

- Likely runtime touchpoints: `agent_controller.py`, `react_loop.py`, `agent_runtime.py`, `builtin_tools/agents.py`, `builtin_executor.py`, `sub_agent_supervisor.py`, `scheduler.py`, memory/result access, Telegram notification and confirmation handlers.
- Planning and verification cover existing supervision, scheduling, permission, prompt-context, result-history, Telegram, and command-surface tests; README/help examples must reflect any changed scheduled-notification wording.
- No new dependency or configurable per-job capability surface is planned. Existing schedule definitions and Telegram management commands remain compatible; two optional read-only `/jobs` subcommands are added. Silent Telegram sends and retained full-result access are new delivery/storage behavior; queued notices are best-effort on delivery failure or process restart, with job history remaining the source of truth.
- `run_now` stays asynchronous with `schedule show` as its outcome follow-up; its existing `notify` semantics are preserved. `parallel` plus preserved context can still lose updates, a pre-existing risk not resolved or silently redefined by this change.

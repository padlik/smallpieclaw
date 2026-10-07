# Exploration brief: isolate job execution

The product has one Telegram operator and one interactive operator session. Keep the existing `/jobs` and `/agents` commands, schedule actions, source visibility, `notify` option, and `skip`/`parallel` overlap contract. Add only read-only `/jobs history <tag>` and `/jobs result <execution-id>` so even a muted or removed job has an operator-accessible result while retained. Do not add multi-user session support, detachable operator work, per-job capability definitions, or changes to depth-1 shell restrictions.

## Alternatives rejected

- Serialize all operator and scheduled work: slow jobs would block the operator and confirmation callbacks need special treatment.
- Detach the current operator turn: transferring live conversation, permissions, and ownership has no justified use case.
- Make every background run independently notify Telegram: on-demand children belong to their parent operator turn.
- Introduce per-job grants: scheduled work instead uses existing default security boundaries and immediately denies any additional approval request.

## Complete execution-policy mapping

| Execution origin | Owner/context | Approval policy | Result consumer | Completion/notification |
| --- | --- | --- | --- | --- |
| Operator turn | Singleton interactive session | Existing interactive confirmation and grants | Operator/user | Turn ends after its on-demand children are terminal and accounted for. |
| On-demand child of operator turn | Child conversation with explicit/transient parent context; parent turn owns lifecycle | May request confirmation through the active operator turn, never beyond current path/tool policy | Parent operator via `get_agent_result` or `wait_for_any_agent` | No independent Telegram completion message or automatic operator result recall; parent waits or cancels before finishing. |
| Scheduled run (cron, once, or `run_now`) | Job-owned task and optional preserved job context only; no reads/writes of operator memory or recall | Default allowed boundaries only; any operation needing further approval is denied promptly, even if an operator turn is active | Execution record; authorised user receives presentation only | Full result available in bounded job history; `notify=false` suppresses both success and failure run-result notices, while launch-failure alerts retain existing behavior. Any due quiet notice waits until operator turn/typing ends, uses expandable quote when it fits, otherwise compact summary and Details access. |
| Plan-step or diagnostic runner | Existing owner and context | Existing policy | Existing parent/diagnostic consumer | No new behaviour in this change. |

`/agents` still lists all currently visible sources; cancellation by ID works for each, and `cancel managed` still targets on-demand plus scheduled. `/stop` retains its existing command contract. Prohibited paths remain prohibited, shell/secret grant vetoes remain, and permitted shared file/tool side effects are not newly sandboxed.

## Cross-module flows

1. `TelegramInterface` admits the one operator turn, starts progress/typing, and invokes `AgentController`/`react_loop`; `builtin_tools/agents.spawn_agent` submits an on-demand child via `SubAgentSupervisor` with parent turn/run identity. Result signaling returns status/output to the parent; finish/stop joins or cancels outstanding owned children before releasing the turn. Confirmation callback reaches the waiting child through the existing depth-0 coordinator without waiting on the interactive run lock.
2. The operator's `schedule` tool or existing job configuration creates/updates a definition; `Scheduler._run_job` starts a scheduled profile through the existing supervisor/runtime boundary, with explicit source/job identity and job-owned input, never the live operator working-memory summary. Job execution cannot consume the operator confirmation coordinator/grants or operator MemoryStore/ResultsMemory. Its complete output/status is held in job-owned execution history for the existing bounded retention period.
3. Scheduler completion records the result first; the Telegram delivery adapter checks the existing `notify`/failure behavior and defers user-facing output while the operator turn/typing is active. Once idle, it sends a silent compact summary plus expandable result if within Telegram's message limit; for longer output, a Details action reads the retained job result without invoking or modifying the operator agent.
4. `/jobs history <tag>` lists retained run IDs, status, and timestamps directly from job history (even after a once-job definition is removed); `/jobs result <execution-id>` retrieves one retained complete result in bounded Telegram output. Both are authenticated, read-only Telegram commands, not operator turns or model tool results; expired records report unavailable.

## Open questions to resolve in design without expanding product scope

- Existing execution history truncates output and retains only a bounded window (currently 48 hours/10 per job): choose how to preserve complete output *within* that window and handle Details after expiry without implying indefinite retention.
- Existing `parallel` overlap with preserved job context can lose updates: retain the published option in this change; note the risk rather than silently changing its semantics.
- Define idle-notification queue behavior on delivery failure/restart while preserving job history as the source of truth; avoid a promise of durable exactly-once Telegram delivery without a durable queue.

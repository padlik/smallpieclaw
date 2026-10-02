## Context

The deployment has one authorised Telegram operator and one interactive conversation. Scheduled runs already have private short-term conversations, but `AgentRuntime` constructs them with the operator's memory/results and shared executor, while the scheduler's spawn adapter may attach live parent context. The ReAct loop publishes child outcomes into shared result recall; the supervisor can notify Telegram independently of the parent turn. The current job execution log truncates result text at 2,000 characters and retains at most 10 entries per job for 48 hours. Existing `/jobs`, `/agents`, `/stop`, `schedule run_now`, `notify`, and overlap policy are public contracts.

In-force ADR context: ADR-0005 assigns submission, result signaling, callbacks and notification initiation to `SubAgentSupervisor`; ADR-0006 owns source visibility/capacity; ADR-0007 makes `AgentRuntime` construction-only; ADR-0026 freezes `PathPolicy`; ADR-0027 makes the depth-0 `ConfirmationManager` the sole grant ledger. Other in-force storage decisions (ADR-0019 and ADR-0028) place job state under the agent's XDG state path and preserve the trusted results exchange surface. None establishes parent-turn versus job-owned execution policy.

The smallest useful C4-inspired **component** view is one process; these are components, not separately deployed services:

```text
                             +--------------------------+
 Telegram operator <-------> | Telegram interface       |
                             | turn admission + idle UI |
                             +-----------+--------------+
                                         | interactive turn
                                         v
                             +--------------------------+
                             | AgentController/ReAct    |
                             | turn ID + finish gate    |
                             +-----+---------------+----+
                                   | spawn         | confirmation
                                   v               v
 +-------------------+     +------------------+  +------------------+
 | Scheduler         |---->| SubAgent         |  | Confirmation    |
 | definitions, runs, |     | Supervisor       |  | Manager/ledger   |
 | outcome policy     |     | lifecycle/owner  |  | active turn only |
 +----+-----------+--+     +--------+---------+  +------------------+
      |           |                 | construct
      |           |                 v
      |           |        +--------------------+
      |           |        | AgentRuntime       |
      |           |        | profile-scoped      |
      |           |        | memory/tool view    |
      |           |        +--------------------+
      |           |                 |
      v           +-----------------+----> job completion signal
 +-------------------+                         |
 | Job execution     |                         v
 | history (XDG)     |                 Telegram idle notice queue
 +-------------------+                 (best effort, silent)
```

- The supervisor remains the background lifecycle owner and initiates callbacks; Telegram only decides *when and how* a notice is presented.
- Runtime profiles select construction-time memory/tool wiring, not source-category visibility or scheduling decisions.
- A scheduled execution may run concurrently with the operator, but shares neither operator memory/recall nor approval grants. The frozen filesystem policy and other permitted tool/file effects remain shared.
- The execution history is the source for scheduled Details; Telegram messages and their queue are never an input to the operator agent.

## Goals / Non-Goals

**Goals:**
- Enforce one **operator turn** as the lifetime and sole model-facing result consumer of its **turn-owned children**; preserve interactive confirmation through the active operator coordinator.
- Give cron, once, and `run_now` **scheduled runs** only **job-owned context** and default file/tool boundaries, with no interactive permission escalation or automatic operator memory/result recall.
- Retain complete outcomes within bounded **job execution records**, and present eligible outcomes silently after the operator is idle, collapsed when they fit and on demand when they do not. Provide read-only `/jobs` history/result access even when notifications are disabled.
- Preserve operator-facing `/agents` inspection/cancellation (including cross-source `managed`), broad `/stop`, schedule actions, visibility, capacity, and overlap interfaces. Model-facing cancellation is deliberately narrower: `cancel_agent` can affect only on-demand children owned by the current operator turn.

**Non-Goals:** Detached operator tasks; per-job capability declarations; multi-user session coordination/pairing changes; new shell permissions; changing `parallel` into `skip`; eliminating shared permitted file/tool effects; durable exactly-once Telegram delivery; changing plan-step/diagnostic ownership.

## Decisions

### 1. Explicit immutable execution owner at admission

Mint a unique immutable turn identity for each interactive ReAct run. The on-demand spawn adapter passes that identity separately from model-facing task/context arguments into supervisor admission, and the supervisor associates admitted on-demand records with the turn. A resumed conversation (`resume_from`) starts a new operator turn with a new unique owner, even when it shares a checkpoint trace; it does not inherit prior-turn children or migrate their outcomes into resumed context. Previous-turn child IDs are inaccessible, with the same actionable, non-disclosing "no child accessible in this turn" response as other inaccessible IDs. A scheduled submission carries job tag/run identity and `scheduled` origin, with no parent turn identity. Model-facing `get_agent_result` and `wait_for_any_agent` return results only for children spawned by that calling turn: the existing council-pattern scenarios for its own children are unchanged, while scheduled outcomes must not enter the operator's model context. A scheduled, non-owned, or unknown result ID returns the same actionable "no child accessible in this turn" result without revealing another run's contents. Model-facing `cancel_agent <id>` and `cancel_agent managed`/`all` may cancel only on-demand children admitted by the calling operator turn. The managed/all aliases always succeed and return the count of that turn's children actually cancelled (zero if none); unlike a specific inaccessible ID, they do not reveal whether scheduled runs or other-turn children exist or how many there are. The model cannot cancel scheduled runs; operator `/agents` inspection/cancellation, including cross-source `/agents cancel managed`, remains available for on-demand and scheduled runs, and `/stop` keeps its existing broad cancellation contract. The source label still drives `/agents` and capacity accounting, not access control; no mutable global "current mode" is used.

At normal `finish`, a turn with any unaccounted on-demand child cannot close: the agent receives actionable feedback to retrieve each completed child's outcome with `get_agent_result` or `wait_for_any_agent`, or cancel a child it no longer needs. These operations use the supervisor's per-turn owned records even after the registry's 300-second TTL, so an owned child cannot permanently block finish due to registry expiry. Consuming an outcome marks it retrieved. Explicitly cancelling a completed-but-unretrieved child discards its result and counts as accounted; a subsequent `get_agent_result` for that owned ID returns an actionable cancelled/discarded status without the result text. A `get_agent_result` timeout with `cancel_on_timeout=true` counts as accounted only if it actually requests cancellation; `cancel_on_timeout=false` and `wait_for_any_agent` timeouts do not. An operator `/agents` cancellation of a turn-owned child also marks it accounted for that turn. Accounted children do not hold up finish for retrieval, but the turn remains draining until their lifecycle cleanup is terminal. On stop, error, or exhausted run budget, request cancellation of remaining children and keep the turn in draining state until their lifecycle cleanup is terminal. Distinguish result readability from lifecycle termination because result signaling currently precedes callback/runner cleanup. Only after cleanup may the controller clear coordinator/prompt tokens and Telegram release the turn; callbacks, `/stop`, and `/agents` are processed independently of the worker wait. `/agents` and `/stop` remain the only cross-source cancellation paths. If cleanup is slow, surface the draining state rather than presenting an orphan as finished.

Suppress independent on-demand completion notifications and auto-publication of child results to operator recall. Their final statuses are returned to the parent via the existing result tools, including failures and cancellations. Preserve the model-facing spawn acknowledgement and response-format behavior.

**Alternative rejected:** Detach children after `finish` or auto-promote their results into future operator context. Both lose the single-consumer boundary. Auto-cancelling on every attempted normal finish hides unfinished work; instead reject finish with a clear wait/cancel choice.

### 2. Construct scheduled work with job-only memory and a noninteractive tool view

`AgentRuntime`'s scheduled profile receives only the job task and the existing optional saved conversation for that job. Internal memory objects required by the ReAct interface must be empty/isolated per run, and its `graph_memory` and `graph_memory_writer` references must remain `None`; no operator `MemoryStore`, `ResultsMemory`, live working-memory summary, or cross-job execution-history prompt is wired into scheduled prompts. Remove the existing automatic scheduled-execution-history section from the **operator's** prompt as well: even a compact summary is not operator context. The shared executor's `memory_write` tool (`get`, `set`, `append`, `delete`) and `memory_graph_search`/`memory_graph_store` are unavailable to scheduled origin; reject their calls with a clear `scheduled_memory_unavailable` error rather than allowing an alternate route to the operator's KV/graph stores. No new per-job KV or graph storage is introduced: `preserve_context` retains only the job's conversation through its existing context-key path. The ReAct result-publication path must not auto-store scheduled or on-demand child output in operator recall. Explicit result persistence remains the scheduler/supervisor callback to job history. Default-available `log_query`/`log_detail` tools remain a separate shared observability surface, not the KV/graph/result-recall memory; this change makes no new isolation claim about their records.

Pass an immutable execution policy with the runner/tool dispatch rather than relying on the shared executor's ambient coordinator or prompt ID. On-demand children may use only their owning *active* turn's depth-0 confirmation channel and its existing grants; path classification and the shell/secret grant veto remain unchanged. Scheduled calls use default allowed paths and tool restrictions, but a required confirmation is rejected **before** grant lookup or Telegram token staging, even when an operator turn happens to be active. The same denial applies to confirmation routes outside ordinary file tools (including vision and MCP-sensitive operations). Prohibited paths continue to fail before any grant logic. Job definition text and model-supplied context are never authority.

Production cron and `run_now` must use that scheduled policy. The legacy direct-agent scheduler fallback fails closed for agentic work when the scheduled executor is unavailable: record an actionable launch failure rather than invoking the singleton operator controller. Empty-task reminders remain direct scheduler notifications, with no agent execution.

**Alternative rejected:** Serialize every run through the operator lock; it delays interactive work and does not give jobs independent memory. Per-job capability schemas would add unneeded policy and operator burden.

### 3. Keep complete results in bounded job history, not the notification

Mint an independent `run-<uuid4 hex>` execution ID when an agentic scheduled trigger passes the overlap guard; it is **not** an `/agents` agent ID or operator prompt ID. Use it to distinguish concurrent `parallel` runs of one tag. Retain the complete returned text, terminal status (done/failed/cancelled), timestamp, existing classification/metadata including failure's next-run information, and job tag in the existing XDG job-execution history, without the present 2,000-character result truncation. A failed launch has that run ID and a failed record with the launch error; a `skip`-policy skipped trigger and an empty-task reminder are not agentic executions and receive neither ID nor record. Preserve the 48-hour and 10-per-job limits; enforce expiry on **read** as well as on the next write. A reused tag shares its existing tag-scoped history until retention expires; unique run IDs prevent confusion between individual executions. Old already-truncated entries lack a run ID: map `success=true` to done and `success=false` to failed for display, list them as legacy/partial in history, but do not claim full Details or fabricate IDs. Details lookup by execution ID returns only retained complete records to an authorised operator; an expired or absent record gives a clear unavailable response. `schedule show` remains the documented metadata follow-up to `run_now` and keeps its existing compact output contract.

Record the outcome before offering notification. Change the current swallowed-write-error path: on a failed history write, log a warning and set the scheduler job's `last_error` to a specific history-persistence failure; if `notify=true`, the queued quiet notice says the result was not retained and Details are unavailable. Do not claim that Details exists when no complete record was persisted. Rewriting full JSONL entries is the smallest compatible storage change, but the count/age limits do not impose a byte limit per result; verify large-output cost and choose a per-execution sidecar at implementation only if measurements justify it, without changing the retention/retrieval contract.

**Alternative rejected:** Use operator `ResultsMemory` as result storage, because it contaminates recall; send the only copy in Telegram, because notice delivery and message length are unreliable.

### 4. Separate supervisor completion from Telegram presentation

The supervisor still signals status, records callbacks, and initiates delivery through a job-owned completion event. Scheduler applies its current `notify` decision: `notify=false` suppresses **all** terminal run-result notices; launch failures remain immediate operator alerts regardless of that flag. Preserve failure classification, error type, next-run content, and no-auto-retry behavior. Completed, failed, and explicitly cancelled run-result notices enter the Telegram-owned idle presentation queue when `notify=true`; timeout-cancelled runs retain the existing stale-notification suppression. Direct empty-task reminders and existing long-running-agent warnings keep their current delivery paths; they are not job-execution results. This preserves the frozen proposal's scoped delivery change.

Telegram marks the turn idle only after the final operator response is sent and the typing task has stopped. Its notification adapter checks idle at send time and serializes the transition against a newly admitted turn; confirmation callbacks never wait on that queue. Send queued run-result notices with `disable_notification=True` (no sound, *not* no delivery). Compare **rendered text after entity parsing**, including header and result, against Telegram's 4,096-character message limit; HTML escaping does not increase the rendered-text allowance. When it fits, render an HTML `<blockquote expandable>` with the summary outside. Otherwise send a short summary plus authorised Details callback resolving the execution ID. For an oversized result, both Details and `/jobs result` deliver the *complete* retained result as an authorised UTF-8 text document, rather than truncating or flooding chat with chunks; short results are shown directly. Escape untrusted HTML, validate callback ownership/expiry, and keep callback data within Telegram's limit. The Details action does not call the agent, create a prompt, or add to operator context. Client rendering and notification-preview concealment are not guaranteed.

The queue is best-effort: if process restart or send failure loses a notice, retained job history is still available via `/jobs history` and `/jobs result`; no exactly-once push guarantee is claimed. Extend the existing authenticated `/jobs` handler with `/jobs history <tag>` (retained execution IDs, timestamp, status, including runs of since-removed once-jobs) and `/jobs result <execution-id>` (one complete retained outcome). Missing arguments return usage; a tag with no active definition and no retained runs reports unknown, while a current job with no retained runs reports empty history; a removed once-job with retained runs is known until those records expire. Unknown/expired run IDs return an unavailable result. These read-only commands do not invoke `agent_handler`, alter any memory, or change existing `/jobs` subcommands. Parallel status tracking must use per-run identity/count so one completion does not falsely mark another parallel execution idle; `schedule show.is_running` stays true while **any** run of that tag is active, even after another parallel run finishes. The published `parallel` option and its preserved-context last-writer-wins risk remain unchanged.

**Alternative rejected:** Sending full results immediately from the supervisor interrupts typing and fragments long messages. Persisting a dedicated delivery queue would add a new durability subsystem beyond the agreed scope.

## Risks / Trade-offs

- [Child cancellation cannot immediately interrupt a non-cooperative operation] -> Keep the turn visibly draining and callbacks responsive; use the existing transport cancellation path and report stuck work rather than claiming clean completion.
- [The model cannot cancel a scheduled run] -> Keep model-facing cancellation limited to current-turn on-demand children; the operator can cancel scheduled runs through `/agents` or the broad `/stop` command.
- [Scheduled runs still consume global capacity but are not model-cancellable] -> Preserve global capacity accounting and have the operator use `/agents` or `/stop` to cancel scheduled work.
- [Scheduled calls might bypass the tool-level policy through an alternate confirmation path] -> Cover file, vision, MCP, and sensitive built-ins with origin-aware integration tests, including a concurrent operator turn and existing session grants.
- [Results stored untruncated enlarge the execution log or expose sensitive text through Details] -> Retain existing count/age bounds, validate large outputs, HTML-escape presentation, and restrict retrieval to authorised operators; do not claim that the current job log redacts arbitrary generated secrets.
- [A quiet notification may be lost across restart or Telegram failure] -> Treat history as authoritative and make delivery best-effort; do not imply that a notice is the sole copy.
- [`parallel` plus preserved job context can overwrite updates] -> Keep this pre-existing contract and document the hazard; no semantics change in this proposal.
- [Legacy saved history contains truncated results] -> Preserve old entries as-is and return an honest unavailable/partial-details indication rather than fabricating missing content.

## Migration Plan

1. Add source/owner-specific policy and turn lifecycle tests before changing runtime wiring; preserve existing source labels and command registration.
2. Introduce isolated scheduled runtime memory and deny scheduled `memory_write`/graph-memory tools, migrate scheduled routing including `run_now` and fail-closed direct fallback, and stop ambient operator history/result injection. Test concurrent scheduled/operator activity and fail-closed confirmation.
3. Add full-result storage and expiry-aware lookup; read existing JSONL without migration, writing the extended record shape only for new runs. Preserve scheduler state/config and existing retention defaults.
4. Move scheduled presentation behind the idle queue, add authenticated Details and `/jobs history`/`/jobs result` read-only views, and update `/jobs` help and README examples. Keep all existing `/jobs` and `/agents` subcommands intact.
5. Rollback can restore the old code against the same schedule definitions; extended JSONL entries remain parseable as dictionaries but rollback loses full-result/Details support. Do not rewrite stored jobs or old ADR files.

## Open Questions

- No blocking product decision remains. The out-of-scope lost-update risk for `parallel` context-preserving jobs may merit a later change; this design does not promise to fix it.
- No in-force ADR needs supersession. Record the new durable turn-owned versus job-owned execution boundary in a new ADR while preserving ADR-0005/0006/0007/0026/0027 responsibilities.

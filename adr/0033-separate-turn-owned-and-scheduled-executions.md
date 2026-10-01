# 0033-separate-turn-owned-and-scheduled-executions

## Status

Accepted

## Date

2026-10-01

## Supersedes

None

## Context

The bot has one interactive operator session but runs on-demand sub-agents and independently scheduled work concurrently. Private sub-agent conversation buffers do not isolate shared memory, results, approval state, or Telegram delivery. A scheduled execution can inherit operator context or grants, and an on-demand child can continue after its parent's confirmation channel and result consumer have gone away. The operator requires scheduled work to remain unattended under existing default security boundaries and its results to stay out of operator recall.

This decision builds on, rather than replaces, ADR-0005's supervisor lifecycle boundary, ADR-0006's visibility/capacity source labels, ADR-0007's construction profiles, ADR-0026's frozen path policy, and ADR-0027's depth-0 grant ledger.

## Considered Options

- Serialize all runs behind the interactive operator lock: keeps one mutable state owner but makes slow jobs block the operator and complicates interactive confirmation.
- Detach operator turns into independently resumable jobs: needs a new state, permission, and result-ownership handoff without a current use case.
- Introduce per-job permissions and shared operator memory: adds capability management and leaves unintended context/result coupling.
- Separate execution ownership by origin, preserving the existing supervisor/runtime responsibilities.

## Decision Outcome

An on-demand sub-agent spawned by the operator is a **turn-owned child**. Its parent **operator turn** owns the child's result and lifetime; the turn must account for or cancel its children before ending. It may request operator confirmation only through that active turn's depth-0 coordinator under the existing policy. It does not independently notify Telegram or publish its outcome to operator recall.

A **scheduled run** (cron, once, or `run_now`) is owned by its job, not by the current operator turn. It uses its job task and optional job-preserved conversation as **job-owned context**, without operator KV, graph memory, result recall, live working memory, or automatic job-history injection. It never borrows operator grants or asks for a new approval: an action needing one fails closed even while the operator is active. Default permitted tool/file effects and depth-1 shell restrictions remain. Its complete outcome is a bounded **job execution record**; Telegram and `/jobs` present that record without injecting it into operator context.

Execution origin and owner are immutable admission data passed through supervision and construction, not inferred from a mutable shared executor field or from the registry's source label. `SubAgentSupervisor` retains submission, lifecycle, callback, and notification initiation; `AgentRuntime` retains construction. This ADR does not redefine the existing `/agents`, `/stop`, schedule, path-policy, or grant-ledger contracts.
The model-facing `get_agent_result` and `wait_for_any_agent` deliver only the active turn's own child results; `cancel_agent` and the operator's `/agents` controls keep their existing managed-source cancellation scope, including scheduled runs. This amends ADR-0005's presentation timing and removes independent on-demand completion messages without superseding its supervisor-owned delivery callback. It restricts which execution origins may consult ADR-0027's ledger without changing grant mechanics, vetoes, or classification-before-grant ordering.

## Consequences

- Good: scheduled work can proceed concurrently without silently adopting operator context or approvals; child results have one consumer and a defined lifetime.
- Good: the existing scheduler/supervisor/runtime boundaries remain useful, with no detachable operator mode or per-job capability schema.
- Bad: origin-aware tool enforcement, result history, and turn-draining behavior span several modules and require race/confirmation regression tests.
- Bad: an uncooperative child can keep its parent turn draining until cleanup, and a best-effort Telegram notice can be lost while the job record remains available.
- Neutral: shared default-allowed file/tool effects, `log_query`/`log_detail`, and the published `parallel` overlap option remain; this is not a full sandbox or a fix for preserved-context last-writer-wins behavior.

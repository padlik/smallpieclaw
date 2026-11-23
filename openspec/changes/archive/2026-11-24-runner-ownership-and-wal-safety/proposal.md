## Why

Current launchers can abandon constructed runners without closing their owned clients, including rejected submissions and accepted work cancelled before execution. Graph startup also deletes WAL sidecars after selected open failures, risking loss of committed, uncheckpointed transactions; both safety findings warrant focused regressions before further runtime cleanup.

## What Changes

- Make launcher ownership explicit from successful construction through rejection or terminal cleanup, including on-demand and scheduled submissions via the supervisor, plan-step and diagnostic launchers.
- Preserve and regression-test constructor cleanup of partially allocated chat and embedding clients.
- Retain queued-task cancellation at shutdown, with exactly-once lifecycle cleanup for accepted tasks that never start; preserve existing started-task cancellation and result/finish behavior.
- Independently attempt tracking rollback and resource cleanup so one cleanup fault cannot skip the remaining stages or mask the primary error.
- Remove application-driven WAL sidecar deletion and repair retries. Graph-open failure leaves graph memory unavailable with actionable log guidance; normal database-engine recovery remains enabled.
- Preserve main-agent operation without graph memory and nonzero live-backfill failure; document backing up the database and all sidecars before operator-led repair.
- Keep this a bounded safety fix, with fewer than 20 implementation tasks. Do not change overload policy, introduce a new executor/lifecycle framework, add Telegram alerts or build destructive repair tooling.

## Capabilities

### New Capabilities

- `graph-memory-startup-safety`: Non-destructive graph opening, optional-component degradation, live-backfill failure and operator recovery guidance.

### Modified Capabilities

- `sub-agent-supervision`: Resource ownership and tracking rollback during submission rejection, accepted-before-start cancellation, and terminal cleanup.
- `agent-runtime-construction`: Caller-owned cleanup of returned runner products across plan-step, diagnostic and direct execution, retaining constructor cleanup for partial allocation. Plan-step active-runner removal and deregistration cleanup scenarios belong here; registry visibility/source/capacity policy remains unchanged.

## Impact

- Runtime: `sub_agent_supervisor.py`, `execution_plan.py`; `agent_controller.py` only if required to preserve its existing owned-client close contract.
- Graph: `graph_memory.py`, `main.py`, `backfill_graph_memory.py`, and relevant README recovery guidance.
- Regressions: supervisor rollback and shutdown, plan/diagnostic visibility, runtime ownership characterization, graph-open unit tests and startup/backfill integration tests.
- No new dependencies, configuration settings, public tool arguments or registry source categories. Direct callers continue to own `.close()`; borrowed root services remain open.
- Availability tradeoff: every graph-open failure, including the formerly repair-triggering `corrupted wal`, `invalid wal record` and `wal_record.cpp` signatures, takes the non-destructive unavailable path; the main agent continues without graph memory. No automatic quarantine, deletion, truncation or recreation is introduced.
- XDG data migration and the separate empty-directory migration remain unchanged.

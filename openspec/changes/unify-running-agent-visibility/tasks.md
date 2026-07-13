## 1. Characterize Current Visibility and Capacity Behavior

- [ ] 1.1 Add or update tests that pin current `SubAgentRecord.source`, `count_managed()`, `cancel_all_managed()`, and `/agents` behavior before changing source semantics.
- [ ] 1.2 Add tests proving scheduled runs currently count against the global supervisor capacity guard and then update those tests to expect `source="scheduled"` while still counting against the guard.
- [ ] 1.3 Add tests proving plan-step and diagnostic runners become visible while preserving existing plan dependency, retry, timeout, and aggregation behavior.

## 2. Update Registry Source and Capacity Model

- [ ] 2.1 Add source-category constants or helpers for `on-demand`, `scheduled`, `plan-step`, and `diagnostic` in `sub_agent_registry.py`.
- [ ] 2.2 Add registry helpers for globally capacity-counted records and update `count_managed()` / `cancel_all_managed()` so `on-demand` and `scheduled` are managed.
- [ ] 2.3 Preserve explicit cancellation by id or label for all visible source categories.

## 3. Update Supervised Run Source Assignment

- [ ] 3.1 Extend supervisor submission inputs so scheduled launches can request `source="scheduled"` while normal model-facing `spawn_agent` uses `source="on-demand"`.
- [ ] 3.2 Update scheduler launch code to pass scheduled source metadata without changing scheduler callbacks, notifications, or result logging.
- [ ] 3.3 Preserve `spawn_agent`, `get_agent_result`, context persistence, timeout cancellation, and graph-memory non-admission behavior.

## 4. Register Plan and Diagnostic Runs

- [ ] 4.1 Register normal plan-step runners in the global sub-agent registry with `source="plan-step"`, cancel-event/LLM-client wiring, iteration metadata, and cleanup in existing terminal paths.
- [ ] 4.2 Register diagnostic/recovery runners with `source="diagnostic"` and cleanup semantics matching normal plan-step records, using `try/finally` around synchronous diagnostic execution so swallowed diagnostic failures do not leave stale records.
- [ ] 4.3 Ensure plan-step and diagnostic records do not count against global `max_subagents` and remain governed by plan-local concurrency controls.
- [ ] 4.4 Preserve existing `PlanExecutor` DAG ordering, retry, diagnostic, timeout, cancellation, and result aggregation behavior.

## 5. Update `/agents` Operator Surface

- [ ] 5.1 Update `/agents` list output to show explicit source labels for `on-demand`, `scheduled`, `plan-step`, and `diagnostic` records.
- [ ] 5.2 Update `/agents cancel managed` behavior and help text so managed means globally capacity-counted sources: `on-demand` and `scheduled`.
- [ ] 5.3 Ensure `/agents cancel <id-or-label>` works for every visible source category.
- [ ] 5.4 Update and test `/status` active-agent count so it includes all visible global-registry records, including plan-step and diagnostic records.

## 6. Verification

- [ ] 6.1 Run focused tests for sub-agent registry, sub-agent supervisor, scheduler launch, execution planning, and Telegram `/agents` command behavior.
- [ ] 6.2 Run `openspec validate unify-running-agent-visibility --type change --strict`.
- [ ] 6.3 Run `ruff check .`.
- [ ] 6.4 Run `vulture . vulture_whitelist.py --min-confidence 80 --exclude interfaces.py,.venv,venv`.
- [ ] 6.5 Run `make check`.
- [ ] 6.6 Perform scripted or manual burn-in for visible on-demand, scheduled, plan-step, and diagnostic records, including cancellation, `/status` count behavior, and cleanup.

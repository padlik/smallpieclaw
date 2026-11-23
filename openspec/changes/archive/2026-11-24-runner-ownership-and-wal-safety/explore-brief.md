# Exploration brief

## Agreed scope

Address section 5 and recommended step 4 of `docs/model-runtime-council-findings.md`: close owned runner resources on every launcher exit and stop application-driven destructive graph recovery. Preserve queued-task cancellation and existing execution/result behavior. Use actionable logs, not a new Telegram warning or repair command. Keep the implementation plan below 20 tasks.

## Ownership mapping

| Phase or launcher | Responsible owner | Required outcome |
| --- | --- | --- |
| Partial runner construction | Constructor | Release allocated chat/embedding clients before propagating construction failure. |
| Returned runner before accepted submission | Supervisor | Close on registration, association or submission rejection; independently undo installed tracking; no execution or finish callback. |
| Accepted task that starts | Supervisor worker lifecycle | Close after execution unwinds; terminalize and finish cleanup once on success, failure or cancellation. |
| Accepted task cancelled before starting | Supervisor accepted-task lifecycle | Never execute; publish cancelled outcome, close, release active capacity, signal cleanup and invoke bound finish callback once. |
| Plan step | Plan launcher | Protect registration, execution, result processing and tracking cleanup from successful construction onward. |
| Diagnostic | Diagnostic launcher | Same protection, independent of deregistration success. |
| Direct synchronous caller | Existing caller | Retain explicit close ownership; do not make `run()` self-closing. |

Supported registry sources remain `on-demand`, `scheduled`, `plan-step`, `diagnostic`; no category, capacity or overload-policy change.

## Cross-module flows

- `SubmissionRequest.factory(**factory_kwargs)` -> runner -> `register_run` -> prompt association -> executor `submit` -> started-worker or cancelled-Future terminal path. Worker and cancellation-only cleanup are mutually exclusive; failed rollback stages must not suppress close or mask the primary error.
- Plan/diagnostic factories -> registration -> synchronous `runner.run` -> outcome handling -> independently guarded deregistration/close, including `_active_runners` removal for plan steps.
- `GraphMemoryStore._open_db(db_path, buffer_pool_size)` -> one ordinary engine open. On failure, application code does not alter the database or sidecars or retry repair.
- `create_graph_memory` -> unavailable store/writer on failure -> main leaves graph unwired; live backfill reports failure, releases its embedding client and exits nonzero.

## Decisions and rejected alternatives

- Remove automatic `.wal`/`.wal.checkpoint` deletion; keep normal native WAL replay. Preserve files for operator-led repair.
- Retain `cancel_futures=True`; draining queued work could start unwanted tasks and delay cleanup behind stuck workers.
- Reject self-closing `run()`: it misses never-started work and changes direct-caller ownership.
- Defer atomic no-backlog admission, new lifecycle frameworks, new notification surfaces, a repair command and changes to the separate empty-directory migration.
- Guidance: stop graph users, back up the database and all sidecars together, investigate/restore with supported procedures, restart; destructive salvage requires explicit operator approval.

## Verification and open questions

Use focused failure-injection and gated concurrency regressions, byte-preservation graph tests and real factory/startup/backfill boundaries. Preserve existing success, cancellation, callback, result-access and borrowed-resource ownership contracts. No blocking product questions remain; implementation details must not expand these policies.

## Artifact progress

- [x] Proposal reviewed and frozen.
- [x] Design reviewed and frozen.
- [x] Specification deltas reviewed and frozen.
- [x] ADR assessment completed and reviewed.
- [x] Tasks reviewed, fewer than 20, and validated.

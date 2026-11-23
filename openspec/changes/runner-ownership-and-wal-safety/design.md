## Context

The frozen proposal addresses two source-confirmed safety defects, not a new runtime architecture. `SubAgentSupervisor.submit` protects association and pool submission but not registration, and rejection rollback does not close the runner. Its accepted Future is discarded, so shutdown cancellation can bypass worker cleanup entirely. Plan and diagnostic launchers also register before their cleanup protection; plan close follows, rather than belongs to, its `finally`.

`GraphMemoryStore._open_db` currently recognizes three WAL error signatures, deletes `.wal`/`.wal.checkpoint` siblings and opens again. This can discard committed, uncheckpointed transactions. Existing graph factory/main boundaries already support unavailable graph memory; live backfill already has an unsuccessful-open exit.

The ADR inventory was reviewed through ADR-0048, including partial supersessions. Governing clauses are ADR-0005 (supervisor lifecycle), ADR-0007 (construction, narrowed by later admission decisions), ADR-0033 (origin/result ownership and bounded draining), and residual ADR-0039 plus ADR-0042 (graph independence, graph-only containment, owned clients and writer shutdown). Nothing mandates application WAL deletion. Source categories, immutable pins and request retry/recovery policy remain unchanged.

### Lightweight C4-inspired component view

```text
smallpieclaw Python process
+--------------------------------------------------------------------+
| Spawn/scheduler --> Supervisor --> existing ThreadPoolExecutor      |
|                         |                   |                      |
|                         |            started / cancelled Future    |
|                         v                   v                      |
|                  registry + owner state <--> terminal cleanup       |
|                                              |                     |
| Plan/diagnostic launcher --------------------+--> owned runner      |
|                                                    |               |
| Main / live backfill --> graph factory              v               |
|                               |              chat + embedding      |
|                               v                                    |
|                         GraphMemoryStore --> Ladybug engine         |
+----------------------------------------------------|---------------+
                                                     v
                                     graph database + WAL sidecars
```

- Components shown are inside one process, not separately deployed services.
- The constructor owns partial allocations; launchers own successfully returned runners.
- Executor acceptance splits rejected submissions from accepted-task terminal accounting.
- Exactly one started-worker or cancelled-Future path performs terminal cleanup.
- Graph failure disables the optional component; application code does not repair its files.
- Assumption: Python Future start/cancel exclusivity is retained; no executor replacement is introduced.

## Goals / Non-Goals

**Goals:** close allocated runner clients on every launcher exit; independently unwind tracking; retain cancellation and accepted-task callbacks; preserve graph recovery data; provide actionable, sanitized log guidance; prove these behaviors with focused regressions.

**Non-Goals:** atomic capacity/no-backlog policy, new task lifecycle framework, new Telegram notification, destructive repair tool, self-closing `run()`, changed source labels, changed ownership/access policy, forced closure of running clients, altered XDG migration or the empty-directory migration.

## Decisions

### 1. Ownership begins at successful factory return

Keep `SubAgentRunner`'s partial-constructor `BaseException` cleanup and explicit `.close()` contract. Start launcher protection immediately after factory return, before registration. Rejection cleanup independently attempts removal of any prompt association, owned-child entry and registry entry that may have been partially installed, plus runner close. A registration helper can insert before raising, so cleanup must work by the constructed runner's identity even without a returned record.

Cleanup faults are logged and do not skip later stages or replace an already-propagating primary exception. Preserve cancellation/exception propagation; do not convert cancellation into ordinary failure. Do not call the accepted-task finish callback for synchronous rejection.

Primary means an exception escaping the protected local operation, explicitly passed to its cleanup boundary; ambient exception-handler state does not establish a primary. An execution error already handled into an outcome is not an escaping primary. After all eligible stages are attempted, preserve the local primary; otherwise propagate the first cancellation/non-`Exception` failure, or the first otherwise-propagating ordinary failure if no cancellation occurred. Preserve existing logged-and-swallowed ordinary result/completion-callback errors. Context persistence, result signaling, lifecycle logging, result recording and eligible completion handoff are independent attempts; terminal-stage failures must not trigger a second terminalization through the execution-error handler.

For plan steps, protect `_active_runners` insertion/removal, registration, run, result processing and deregistration with an encompassing cleanup boundary. Diagnostic launchers follow the same pattern without plan-only tracking. Close must be attempted even if deregistration fails. Existing direct synchronous callers remain responsible for explicit close; root-shared services are never closed by a child.

Alternative rejected: putting close in `run()` would miss rejection and queued cancellation, and change direct-caller ownership.

### 2. Retain queued cancellation; finalize outside executor locks

Retain `shutdown(wait=False, cancel_futures=True)` and existing bounded cooperative shutdown. Keep a supervisor-local map of outstanding accepted entries containing request/options, runner, record and submitted Future. Use one submission lock and a stopping flag to serialize only the final submission handoff against shutdown. After construction, registration and association, acquire that lock, reject if stopping, otherwise insert the pending entry, call `pool.submit`, attach the returned Future to the entry and release the lock. Submission failure removes the pending entry before the independent rejection rollback. No finish, notification, context-save, close or other user callback runs while this lock is held.

The worker's outermost cleanup retires its entry under the submission lock after all terminal stages, including when cleanup itself fails. Insertion precedes submission so an immediately finishing worker cannot retire a not-yet-inserted entry; lock release follows Future assignment so shutdown cannot observe an incomplete accepted entry. Do not perform new fallible setup after successful submission that could incorrectly trigger rejection cleanup.

Never nest the submission lock with `_owned_lock` or registry locks in either direction. Rejection rollback, including prompt removal, owned-state removal, deregistration and runner close after submit failure or stopping rejection, runs only after releasing the submission lock.

At shutdown, set stopping and snapshot accepted entries under the submission lock, then release it. Signal cancellation to the snapshotted records while preserving the existing registry cancellation scope. Call `pool.shutdown(wait=False, cancel_futures=True)` outside the submission lock before beginning the bounded cooperative drain. Once executor shutdown returns, claim entries whose Futures report `cancelled()` under the submission lock and remove them from outstanding tracking; perform their terminal cleanup outside all locks before draining started work. Repeated shutdown must not claim the same entry twice. A failure or cancellation in one claimed entry must not skip the other claimed entries or the attempted bounded drain; defer propagating cancellation until these attempts finish.

The claim-and-drain sequence starts with no local primary, regardless of an unrelated caller's exception handler. Across that sequence, retain the first cancellation/non-`Exception` failure over otherwise-propagating ordinary drain failures. Ordinary per-entry finalization failures remain logged and swallowed; attempt every claimed entry and the bounded drain before propagating the selected failure. Only supervisor shutdown cancels accepted Futures; timeout and owner cancellation continue to signal records, not directly cancel stored Futures.

Reject late submission handoffs with `SupervisorStoppingError`, a dedicated `RuntimeError` subtype in `sub_agent_supervisor.py`, after independent rollback outside the submission lock. Direct supervisor callers retain the exception-based rejection contract. Only the spawn compatibility shim translates this deliberate stopping subtype into the existing structured refusal shape: `success=false`, empty `output`, `exit_code=-1`, `error_type="impossible_with_current_tools"`, `recoverable=false`, a fixed safe stopping message, actionable operator restart guidance and an execution disposition of `refused`. Never match exception text or convert generic submission/registration errors or cancellation into this refusal. Scheduled launches pass through the same shim; retain their existing generic safe launch-failure presentation and exactly-once run release rather than adding a scheduler error-template allowlist entry. No accepted-task finish callback runs for rejection.

Queue-first terminalization removes delay caused by queued records, not legitimate waiting for started workers. Running workers may still consume the existing bounded cooperative drain, and synchronous cleanup callbacks gain no new time bound. Keep source categories, capacity and overload policy unchanged.

Do not install a terminal-cleanup Future callback. CPython can execute cancellation callbacks inline while holding the executor's non-reentrant shutdown lock. A started Future cannot successfully cancel and remains worker-owned; a cancelled Future never invokes its runner. Shutdown handles only cancelled entries, never closes a started runner early, and does not wait for a stuck worker to free capacity before closing a cancelled queued runner. Outstanding tracking contains only not-yet-cleaned work, not all historical Futures.

The cancellation-only path records the existing cancelled status/sentinel, saves requested context before result signaling, applies existing result-log/completion routing, deregisters active capacity, closes the runner, expires grant scope, invokes bound finish once and signals owned cleanup. Preserve timeout-cancelled notification suppression and existing `completion_cb`/notify gating unchanged. Omit worker-only `bind_run_context`/`clear_run_context`; the shutdown thread must not erase its caller's thread-local context. Preserve retained owner-result semantics: release active tracking, not terminal records required by the owner. Notification failure never reruns work.

Alternatives rejected: full cleanup in a done callback (executor-lock deadlock/reentrancy risk); drain queued work (unwanted late execution and stuck-worker delays); assume queueing is impossible (capacity checks are not atomic and direct submissions exist); custom executor (unnecessary scope).

### 3. One normal graph open, no application repair

Simplify `_open_db` to one normal Ladybug database construction. All open errors, including the formerly recognized WAL signatures, propagate to containment without application-driven retry, removal, truncation, renaming, quarantine or recreation. Preserve native successful open/replay and fresh-store creation.

Containment emits fixed, sanitized guidance: graph memory is unavailable; stop graph users; back up the database and all sidecars together; investigate/restore using supported procedures; restart after repair. Destructive salvage requires explicit operator approval outside this change. At graph-open failure boundaries, log only the exception class beside the fixed guidance, not arbitrary engine exception text, `repr` or traceback containing it; tests inject a secret-bearing exception to prove it stays absent.

At `create_graph_memory` failure, return unavailable store/writer and start no writer. Main continues with graph unwired; ensure at least one actionable warning survives the actual factory boundary rather than only an exception-class message. Live backfill exits nonzero before import/extraction, closes its owned embedding client and preserves source/state files. Existing dry-run behavior remains unchanged.

File preservation means the application performs no recovery mutation after a failed engine open; it cannot guarantee that a native engine did not modify files during its normal open attempt. Tests inject a failing engine that does not mutate files, then prove byte equality and no application repair calls. Do not claim a corrupted-database transaction-loss reproduction.

Connection/schema/vector-extension failures after successful database open are outside this repair-removal change. Remove obsolete WAL-specific markers if unused; retain shared error matching used by vector/schema code.

### 4. Evidence path and verification ownership

| Claim | Evidence owner during apply | Minimum evidence |
| --- | --- | --- |
| Rejected runners close without stranded tracking | Runtime implementer | Inject association/submission/registration failures, including insert-then-raise and cleanup failures; close count, no run/finish, independent rollback attempts. |
| Accepted queued cancellation completes once | Runtime implementer | Gate one real executor worker, queue a second task, cancel via shutdown; no second run, cancelled outcome, close/finish/result signaling and active-capacity release once; repeat shutdown and callback re-entry into submit completes without deadlock. |
| Started work and shutdown race stay safe | Runtime implementer | Gated submit/shutdown and running-cancellation tests; no close-before-unwind, no orphaned accepted task, unchanged success/failure/callback routing. |
| Plan/diagnostic exits release resources | Runtime implementer | Registration, run, result-processing and deregistration failure injection; close attempted and independent tracking cleanup. |
| Ownership stays compatible | Runtime implementer | Existing partial-construction/direct-caller tests plus borrowed root-resource and repeated-close assertions. |
| Failed graph open is non-destructive | Graph implementer | Former WAL signatures and unrelated open errors; one open, unchanged seeded DB/sidecars, no Connection/schema/writer and no repair calls. |
| Real caller boundaries degrade correctly | Graph implementer | Actual factory/main wiring and live CLI backfill with injected engine failure; safe guidance, unavailable graph, exit failure and embedding close, unchanged source/state. |
| Integrated result meets repository gates | Final command executor | Focused tests, `ruff check .`, `vulture . vulture_whitelist.py --min-confidence 80`, and `make check`, preserving the current skip baseline. |

Reuse existing pytest/unittest.mock fixtures; use Events/barriers and bounded waits, not timing sleeps or a new testing dependency. A synthetic Future-only test is weaker and insufficient alone for shutdown; destructive corruption experiments are unnecessary and unsafe for this claim. Each regression must be observed failing before its fix; run relevant checks after meaningful code changes and final checks after reconciliation.

## Risks / Trade-offs

- [Executor cancellation runs callbacks under its own lock] -> No terminal-cleanup done callback; finalize cancelled entries only after executor shutdown returns, outside submission and executor locks. Test a finish callback that re-enters submit and receives rejection without deadlock.
- [Close/deregistration/callback fails] -> Independent cleanup stages preserve the primary error and still attempt close/finish/signaling.
- [Uncooperative started task] -> Keep bounded shutdown/drain and cooperative cancellation; do not force-close active transports.
- [Reduced graph availability] -> Intentionally prefer retained recovery data over automatic destructive salvage; actionable logs and README guidance explain the tradeoff.
- [Scope growth] -> Keep overload policy, repair tools, notification UX and empty-directory migration out of the fewer-than-20-task plan.

## Migration Plan

No configuration, data-schema or dependency migration. Replace tests that currently expect leaked rejection clients or WAL deletion/reopen; retain ordinary graph-open and execution-success coverage. Update README recovery guidance before rollout. Previously deleted WAL data cannot be restored by this change.

Deploy through the normal verified change workflow. On graph-open failure, keep files intact and repair offline; do not roll back to destructive recovery against an affected live database. If an application rollback is necessary, disable graph memory first and preserve backups.

## Open Questions

No blocking product decisions. Use the agreed lightweight ASCII diagram. No in-force ADR requires supersession; ADR assessment will record preservation of existing ownership and graph-only degradation boundaries. Local helper layout remains an implementation choice within these constraints.

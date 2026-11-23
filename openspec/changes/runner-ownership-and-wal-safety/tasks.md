## 1. Establish ownership regressions and fix rejection

- [x] 1.1 Preserve/extend runtime ownership characterization in `tests/test_agent_runtime_characterization.py` and existing allocation-owner tests: partial allocation failure after embedding/chat allocation, explicit direct-caller close, repeated close and borrowed root services staying open. Run the focused cases and record their baseline.
- [x] 1.2 In `tests/test_sub_agent_supervisor.py`, reverse the rejection expectation that currently leaves `runner.closed` false; add on-demand/scheduled association, executor, registration-before-insert and insert-then-raise failure cases, plus rollback-stage failure injection. Assert one close attempt, independent tracking rollback, primary-error preservation and no execution/finish callback. Observe the changed behavior tests failing before fixes.
- [x] 1.3 Protect supervisor ownership from factory return through registration/association/submission; close rejected runners and independently attempt all installed-state rollback without masking primary errors. Make 1.2 green, preserving safe admission errors and cancellation propagation.

## 2. Cover accepted queued cancellation and shutdown handoff

- [x] 2.1 Add gated real-executor regressions in `tests/test_sub_agent_supervisor.py`: blocked first worker, queued second task, shutdown cancellation, cancelled result/context-before-signal ordering, one close/grant/finish/owned-cleanup attempt, capacity release, retained owner outcome and repeated-shutdown idempotence. Include cleanup/callback failure cases and observe failures before implementation.
- [x] 2.2 Before implementing the handoff, add regressions for immediate worker completion, submit/shutdown interleaving and plain post-shutdown rejection with rollback. Observe the defect cases failing and record already-correct baseline cases separately; use Events/barriers and bounded joins, never sleeps as synchronization.
- [x] 2.3 Implement outstanding accepted-entry tracking and submission-lock/stopping handoff per design Decision 2; insert before submit, assign Future before release, reject late handoffs with rollback outside the lock, and retire worker entries after outermost cleanup. Keep source/capacity policy unchanged and make the targeted 2.2 handoff cases green.
- [x] 2.4 After executor shutdown returns, claim cancelled queued entries once and run terminal/result/context/cleanup stages outside all locks, preserving completion/notify/timeout gates and omitting worker thread-local clearing. Do not close started clients early or install terminal-cleanup Future callbacks. Make 2.1 green.
- [x] 2.5 Add edge regressions for a finish callback re-entering submit, shutdown caller context preservation and cooperative running cancellation without early close. Observe any defect failure before fixing it; preserve already-correct baseline behavior. Require bounded joins, no deadlocks and no duplicate effects.

## 3. Protect plan-step and diagnostic launchers

- [x] 3.1 Extend `tests/test_running_agent_visibility.py` and relevant `tests/test_execution_plan.py` cases with retained runner close counts and injected plan/diagnostic registration-before/after-insert, execution, outcome-processing and deregistration failures. Assert independent close/tracking cleanup, `_active_runners` removal where applicable, and primary-error/cancellation preservation; observe defect regressions fail.
- [x] 3.2 In `execution_plan.py`, encompass each returned plan/diagnostic runner's registration, execution and result handling in independently guarded cleanup. Keep runner close inside protected cleanup even when deregistration fails; remove plan active entries independently. Make 3.1 green without changing construction or execution/result semantics.

## 4. Remove destructive graph recovery

- [x] 4.1 Replace deletion/reopen expectations in `tests/test_graph_memory.py` with failing-first one-open and byte-preservation cases for all three former WAL markers, absent sidecars and unrelated open errors. Assert no application repair operations or post-failure Connection/schema creation; retain ordinary successful/fresh-store open coverage.
- [x] 4.2 Change `GraphMemoryStore._open_db` to one normal engine-open attempt with no application file repair or retry. Remove unused WAL-specific markers, retaining shared error matching still used elsewhere. Make 4.1 green; leave native replay, XDG migration and the separate empty-directory migration unchanged.

## 5. Prove real graph failure boundaries and guidance

- [x] 5.1 Add failing-first integration cases in `tests/test_graph_memory_integration.py`, relevant `tests/test_main.py` startup harness and `tests/test_graph_memory_backfill.py`: inject open failure through the real factory/CLI boundaries, verify graph unavailable with no writer/extraction, main still operating, live-backfill nonzero exit with embedding close and source/state preservation. Inject secret-bearing engine text and capture logs/output.
- [x] 5.2 Update graph factory/main/backfill failure reporting as needed so fixed actionable unavailable/backup/repair guidance survives the real boundaries, logging exception class only, never raw text/repr/traceback; make 5.1 green. Preserve dry-run non-effects and existing owned-client/writer shutdown ordering.

## 6. Document and verify the integrated safety fix

- [ ] 6.0 Resolve the approved panel warnings against the revised design: retain explicit-local-primary and terminal-precedence regression evidence; observe failing-first queue-before-drain and typed stopping/shim-refusal regressions, then implement those two remaining fixes. Preserve started-worker ownership, cancellation precedence, no locked callbacks, direct RuntimeError-compatible rejection, unexpected-error propagation and unchanged generic scheduler launch-failure/release behavior. Verify exact refusal fields, multi-entry cancellation through the bounded drain, repeated shutdown and reentrant submission. Update the sub-agent usage section in `README.md` and reconcile all four warning fixes before final verification.

  Execute the remaining work as small red/green slices: first the real-executor queued-cleanup ordering and drain/error-selection cases, then direct `SupervisorStoppingError` and shim-only translation with scheduler compatibility. Record exact commands, actual exit statuses and failing assertions before each production correction. Keep the already-reviewed warning 1–2 evidence distinguished from later characterization; never relabel retrospective runs as prospective TDD.

- [x] 6.1 Update README graph recovery guidance to describe graph-only degradation, backing up the database and all sidecars together after stopping graph users, supported offline investigation/restore and restart, and explicit operator approval for destructive salvage. State that no Telegram alert or automatic repair is added; do not document hypothetical tools.
- [ ] 6.2 Reconcile runtime and graph changes and run the focused regression command below once on the final combined state; include the files containing all new shim and scheduler regressions if additional to the listed files. Record commands, exit statuses, scenario-to-test mapping and evidence, including cancellation/notification and borrowed-resource compatibility. Reuse still-valid earlier evidence; rerun affected cases after repairs.
- [ ] 6.3 Run `ruff check .`, `vulture . vulture_whitelist.py --min-confidence 80` and `make check` on the final state with the existing project environment. Investigate failures without weakening checks or adding skips; require no new skips relative to the actual pre-change baseline. Capture exact commands, exit statuses and relevant log excerpts.
- [ ] 6.4 Run `openspec validate runner-ownership-and-wal-safety --type change --strict` and verify that implementation/evidence matches all three deltas without scope creep. Request the separate verify/merge/archive workflow as appropriate; do not commit, merge or archive without explicit approval.

### Execution notes

19 tasks total; each is bounded to at most two hours. No split is needed. The supervisor/plan ownership work and graph work are independently implementable areas, but parallel writers require separate worktrees and reconciliation before final validation. Stay off `main` for code changes, and start apply only after the approved planning state is committed and reaches `main`. Task 6.0 precedes final verification; its revised shutdown/rejection decisions must reach main before their implementation.

Use strict red-green cycles for new behavior: do not write production fixes before observing their regression failures. If a task contains several edge cases, implement them as small individual cycles rather than writing all fixes first. After every code change run required static checks and relevant tests; the final gate is in 6.3. These tasks use the existing pytest stack, not a new acceptance runner.

Focused command (existing venv on PATH; no installs):

```bash
export PATH="$PWD/.venv/bin:$PATH"
pytest tests/test_sub_agent_supervisor.py tests/test_running_agent_visibility.py tests/test_execution_plan.py tests/test_agent_runtime_characterization.py tests/test_graph_memory.py tests/test_graph_memory_integration.py tests/test_graph_memory_backfill.py tests/test_main.py tests/test_trusted_scheduled_spawn.py tests/test_scheduler_failure_classification.py -v --tb=short
```

Runtime implementers own evidence for 1–3; graph implementers own 4–5 and documentation; the final command executor owns 6.2–6.4 after reconciling all writers. No destructive graph experiment or production database is needed.

### Approved execution deviation

The user explicitly approved continuing with the recorded test-first sequencing exception for tasks 2.1, 2.2 and 2.5: some scenarios were added after the initial implementation. No retrospective test-first claim is made. The queued-cancellation and submission-during-drain defects were observed before their fixes; subsequent cancellation-isolation fixes used prospective red/green tests, though their exact red exit statuses were not captured. The reviewed final behavior passed 344 focused tests and both required static checks. Remaining implementation must use strict test-first sequencing.

The user separately approved retaining the factory-reporting fix in tasks 5.1–5.2 with its sequencing exception recorded: the backfill secret-leak test preceded the shared guidance/factory edits, but the factory-specific test followed them. A later temporary-revert demonstration is not claimed as prospective TDD. Missing full-startup/request and existing-file CLI preservation coverage was subsequently added, passed, and reviewed.

### Panel-warning revision — 2026-11-23

The original 18 tasks reached completion with 550 focused and 6427 full-suite passes, plus the same two pre-existing skips. Those results remain historical evidence, not fresh verification of subsequent warning fixes. The user approved revising the existing change; design and supervisor/runtime specs were re-reviewed, and the ADR manifest was revalidated unchanged. Task 6.0 adds the approved follow-up while preserving the original task IDs and history; 6.2–6.4 are reopened for the amended state.

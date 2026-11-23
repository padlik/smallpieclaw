# Implementation evidence

## Scope and ownership

Implementation is on `fix/runner-ownership-and-wal-safety`. Original planning commit `5a862df` was already on `main`; the user subsequently approved the reviewed planning-only revision commit `6f262eb` and its fast-forward onto `main`. The five-file planning checkpoint preserved all pending implementation/evidence bytes. No implementation commit, implementation merge, push or archive is authorized or performed.

Runtime changes are limited to supervisor lifecycle and plan/diagnostic cleanup. Graph changes remove application WAL repair and provide fixed class-only failure guidance at the factory and live-backfill boundaries. Main composition remains unchanged; a real startup-harness regression proves graph-unavailable request continuation. README describes implemented recovery behavior.

## Baseline

With the existing `.venv/bin` on PATH, `make check` exited **0**: **6335 passed, 2 skipped**, 109.17 seconds. Ruff and the Makefile Vulture invocation passed.

The two pre-existing skips are:

- `tests/test_consent_scope_wording.py::test_vision_query_scope_line`: intentional placeholder.
- `tests/test_path_policy.py::TestIsContained::test_normcase_case_insensitive`: macOS `normcase` behavior.

Full local baseline log: `/private/var/folders/6s/p9xmtzd53wv7jx8zq89wqzmr0000gn/T/opencode/runner-safety-baseline.log`.

## Scenario-to-test traceability

| Contract | Tests and observations |
| --- | --- |
| Partial allocation, explicit/repeated close, run does not self-close, borrowed services remain open | `tests/test_agent_runtime_characterization.py::TestSubAgentRunnerOwnership`; constructor chat/embedding failures, both transports checked on explicit close and success/failure run paths. Existing behavior characterization, no manufactured red. |
| Rejected on-demand/scheduled submissions | `tests/test_sub_agent_supervisor.py::TestRejectedSubmissionOwnership` and `TestAdmissionRollback`; association, executor and registration-before/after-insert failures; every rollback stage independently attempted once; primary exception identity and no run/finish. Initial red: 9 failed/1 passed, exit 1. Cleanup-cancellation red aborted with `KeyboardInterrupt`, exit 2; green 4 cases. |
| Queued cancellation, context before result signal, capacity release, retained owner result, notification gates, repeat shutdown | `TestQueuedCancellationTerminalCleanup` in the supervisor tests; real executor with blocked first worker and queued work. Initial red: expected cancelled status but observed running, exit 1. Scheduled result-log/completion and timeout suppression, on-demand no-notify, owned outcome and stage failures covered. |
| Submission/shutdown handoff and immediate completion | `TestShutdownHandoff`; stopping-during-drain red did not raise rejection. Entry retirement, plain late rejection and an Event/probed-lock handoff establish acceptance-or-rollback behavior. No terminal Future cleanup callback. |
| Callback reentry, caller context, started clients | `TestShutdownEdges`; bounded joins, finish callback submits and receives rejection, shutdown caller context remains bound, started runner closes only after unwind. |
| Cancellation-derived cleanup faults and multiple queued entries | `TestCancellationIsolation`; context save, close, grant/result/completion/finish callbacks and owned signaling faults. All claimed entries still receive remaining cleanup attempts; deferred cancellation propagates afterward. Prospective red assertion excerpts recorded; exact red exit statuses were not captured. |
| Primary cancellation survives paired cleanup failures | `TestPrimaryCancellationPreserved` in the supervisor tests; primary context/execution cancellation paired with cleanup cancellation or owned-signal ordinary failure retains the original exception object and retires the entry. Missing terminal behaviors observed failing before their minimum fixes; additional execution-path pre-copy comparison is post-hoc compatibility evidence, not prospective TDD. |
| Explicit local primary and independent terminal failure precedence | `TestExplicitPrimaryCleanup`, `TestCancelledEntryExplicitPrimary`, `TestTerminalPrecedence` and diagnostic/plan ownership tests; unrelated outer handlers do not suppress cleanup cancellation, handled outcomes are not propagating primaries, completion cancellation outranks an ordinary signal failure, all five terminal stages are independent, and terminal faults do not re-enter execution-error finalization. Prospective missing-behavior reds are recorded separately from later compatibility guards. |
| Queue cleanup before started-worker draining | `TestQueueFirstShutdown` in supervisor tests; a real blocked worker remains open while queued close/finish precede drain entry. Multi-entry cleanup cancellation, later drain faults, unrelated caller handlers and repeat shutdown exercise cancellation identity and once-only effects. Ordering red preceded the reorder; additional compatibility guards were added afterward and are not claimed as prospective TDD. |
| Typed stopping rejection and spawn-only refusal | `TestShutdownHandoff::test_post_shutdown_submission_raises_typed_stopping_error`, `TestStoppingRefusalAtSpawnBoundary` and `tests/test_trusted_scheduled_spawn.py::test_scheduled_launch_against_stopping_supervisor_is_generic_failure_released_once`; direct RuntimeError-compatible subtype, exact refusal fields/disposition, unexpected errors/cancellation unchanged, existing generic scheduler presentation and once-only release. |
| Plan/diagnostic ownership and independent cleanup | `tests/test_running_agent_visibility.py::TestDiagnosticRunnerOwnership`, `TestPlanStepRunnerOwnership`, `TestPlanStepCleanupIndependence`; close counts, registration before/after insertion, active insertion-after-mutation/removal faults, outcome/execution/cancellation errors, cleanup cancellation, logged close faults and borrowed services. Prospective red exits 1 for each missing behavior; green focused run 177 passed. Existing `tests/test_execution_plan.py` retained. |
| One native graph open; no application repair | `tests/test_graph_memory.py::TestGraphMemoryOpenNonDestructive`; all three former WAL markers, absent sidecars, unrelated error, seeded bytes, no Connection after failed construction, successful open. Prospective red exit 1: two opens instead of one. Green graph unit run 98 passed. |
| Real graph factory containment and safe guidance | `tests/test_graph_memory_integration.py::test_real_factory_open_failure_is_graph_unwired_with_fixed_guidance_only`; injected secret-bearing engine failure traverses real store/factory, no writer, unwired graph, preserved artifacts and fixed guidance/class only. |
| Actual startup serves ordinary requests without graph | `tests/test_main.py::TestRootStartupGraphMemory::test_real_store_open_failure_keeps_startup_serving_and_ordinary_request`; real startup/factory/store, failing engine, Telegram serving reached, ordinary request returns `answer-1`, graph unwired, no writer, artifact preservation and secret absence. Existing-correct characterization. |
| Live backfill nonzero exit, owned embedding close, source/state/artifact preservation | `tests/test_graph_memory_backfill.py::TestBackfillMainLiveAndDryRun::test_live_open_failure_exits_nonzero_with_fixed_guidance_and_no_secret`; actual CLI/store path, seeded DB/WAL/checkpoint plus source/state, no import call, one embedding close, sanitized logs/output. Prospective backfill red exit 1 exposed secret in raw engine error. Existing dry-run tests remain unchanged. |

Graph preservation tests use a nonmutating injected engine. They establish absence of **application-driven recovery mutation**, not native-engine behavior on an actually corrupted database. No production database or destructive corruption experiment was used.

## Focused evidence before integration

All commands used the existing project environment, with no installations or rule changes.

- Ownership characterization: `pytest tests/test_agent_runtime_characterization.py -q` — exit 0, 89 passed.
- Rejection cleanup: `pytest tests/test_sub_agent_supervisor.py tests/test_agent_runtime_characterization.py -q` — exit 0, 154 passed at that stage.
- Final supervisor slice plus ownership/context/confirmation/scheduled-spawn regressions — exit 0, 344 passed.
- `pytest tests/test_running_agent_visibility.py tests/test_execution_plan.py tests/test_agent_runtime_characterization.py -q` — exit 0, 177 passed.
- `pytest tests/test_main.py tests/test_graph_memory_integration.py tests/test_graph_memory_backfill.py tests/test_graph_memory.py -q` — exit 0, 275 passed.
- Exact `ruff check .` and `vulture . vulture_whitelist.py --min-confidence 80` passed after the final production/test slice. An earlier implementer-supplied excluded Vulture command was not accepted as the required gate; exact commands were independently run successfully.

Detailed local reports, scoped review diffs and logs are retained in `.superpowers/sdd/tasks/`. Each implementation slice received independent Oracle spec/code-quality review; outstanding correctness findings were fixed and scoped re-reviewed clean.

## Approved execution exceptions and decisions

- No automatic commits despite generic implementation-workflow defaults; explicit project/user approval controls Git operations.
- Regression tasks were coupled with their dependent implementation task in sequential dispatches; no concurrent production writers.
- Internal tracking fault injection is required by these specifications despite generic boundary-only mocking guidance.
- The user approved the recorded test-first sequencing exception for tasks 2.1, 2.2 and 2.5. Some tests followed implementation; interim passing cases are not described as pre-change baselines. The subsequent cancellation-isolation fixes used prospective red/green assertions, with missing exact red exit statuses disclosed.
- The user separately approved retaining the factory-reporting fix despite the factory-specific test following its edit. The backfill leak test preceded the shared edits; a later temporary factory-line revert is not claimed as prospective TDD.
- Nonblocking review note: the submission lock is reentrant to support existing inline executor doubles. The concrete production `ThreadPoolExecutor` does not execute inline; production cleanup/callbacks remain outside the handoff lock. Real-executor tests provide the shutdown evidence.

## Original integrated gates (before panel-warning follow-up)

All commands ran in the project root with `.venv/bin` on PATH, after reconciling all production/test writers.

| Command | Actual exit | Result |
| --- | --- | --- |
| `pytest tests/test_sub_agent_supervisor.py tests/test_running_agent_visibility.py tests/test_execution_plan.py tests/test_agent_runtime_characterization.py tests/test_graph_memory.py tests/test_graph_memory_integration.py tests/test_graph_memory_backfill.py tests/test_main.py -v --tb=short` | 0 | 550 passed, no skips, 22.80 seconds |
| `ruff check .` | 0 | All checks passed |
| `vulture . vulture_whitelist.py --min-confidence 80` | 0 | No findings; no extra flags |
| `make check` | 0 | 6427 passed, 2 skipped, 112.68 seconds |
| `openspec validate runner-ownership-and-wal-safety --type change --strict` | 0 | Change is valid |
| `git diff --check` | 0 | No whitespace errors |

The full suite has exactly the two baseline skip identities listed above, with no new skips. Full amended-code logs and actual exit-status report are retained as `.superpowers/sdd/tasks/amended-final-{focused,ruff,vulture,make-check,openspec-validate,git}.log` and `amended-final-validation-report.md`. Earlier `final-*` logs retain the pre-correction gate (546 focused / 6423 full-suite passes).

Whole-change review found one paired-failure cancellation-preservation gap in supervisor terminal cleanup. That correction received a clean scoped re-review before the original gate above. A subsequent full panel approved the pending changes with nonblocking warnings; the user then requested their remediation. The original gate is historical evidence, not final verification of the later warning fixes.

## Panel-warning follow-up

- Revised design, two affected specs and tasks passed sequential reviews; the ADR manifest was revalidated unchanged. Strict validation passed after splitting an overlong requirement without changing semantics. The approved five-file planning commit is `6f262ebed0abb86dd8b44dd94ef04aa51561b59c`, present on `main` before queue-first/refusal implementation.
- Warnings 1–2 (explicit local primary and terminal failure precedence) passed independent scoped review after completion-cancellation and lifecycle-log independence corrections. The focused command recorded in `.superpowers/sdd/panel-warning-fixes/correctness-report.md` exited 0 with 513 passed; exact Ruff/Vulture checks exited 0. Prospective red tests and later already-correct compatibility tests remain distinguished.
- Before the planning commit, exact Ruff/Vulture, `make check` and strict OpenSpec validation all exited 0. The full suite had 6440 passed and the same two baseline skips. Reports/logs are in `.superpowers/sdd/panel-warning-fixes/checkpoint-*`.
- Warnings 3–4 (queue-first shutdown and shim-only structured stopping refusal) are implemented on the fix branch and passed independent production/spec review. Two test-only correction rounds added retained scheduled-runner close/no-run assertions and per-entry result/owned signaling, result/finish/grant/completion counts, actual result events and context-before-signal assertions. The final scoped re-review passed with no remaining findings. Earlier focused runs and all prospective/post-hoc distinctions are retained in `shutdown-refusal-report.md`. All four requested warning fixes and final checks 6.0–6.4 are complete.
- No separate OpenSpec verify/archive stage or implementation commit/merge is implicitly authorized by the planning checkpoint.

## Historical four-warning remediation gate — 2026-11-23

All production/test writers for the original four-warning remediation finished before this gate. These results describe that checkpoint, not the subsequent three-warning fixes below.

| Command (existing `.venv/bin` on PATH) | Actual exit | Result |
| --- | --- | --- |
| `pytest tests/test_sub_agent_supervisor.py tests/test_running_agent_visibility.py tests/test_execution_plan.py tests/test_agent_runtime_characterization.py tests/test_graph_memory.py tests/test_graph_memory_integration.py tests/test_graph_memory_backfill.py tests/test_main.py tests/test_trusted_scheduled_spawn.py tests/test_scheduler_failure_classification.py -v --tb=short` | 0 | 673 passed, 0 skipped, 24.65 seconds |
| `ruff check .` | 0 | All checks passed |
| `vulture . vulture_whitelist.py --min-confidence 80` | 0 | No findings; no extra exclusions |
| `make check` | 0 | 6450 passed, 2 skipped, 113.61 seconds |
| `openspec validate runner-ownership-and-wal-safety --type change --strict` | 0 | Change is valid |
| `git diff --check` | 0 | No whitespace errors |

The two full-suite skips are exactly the original consent-vision placeholder and macOS normcase cases; there are no new skips. Full actual-exit report and logs: `.superpowers/sdd/panel-warning-fixes/final-validation-report.md` and `final-{focused,ruff,vulture,make-check,validate,apply,git}.log`.

Independent scoped review accepted explicit-primary/precedence fixes and queue-first/refusal integration; both test-evidence corrections were re-reviewed clean. No requested warning remains outstanding. The optional cosmetic blank-line note was not part of the four requested fixes.

`main` and the fix branch share planning commit `6f262eb`; all implementation and its updated evidence remain uncommitted on the fix branch. The next lifecycle action is separate OpenSpec verification, not an implicit commit, implementation merge, push or archive.

## Subsequent three-warning remediation — completed

The user approved the narrow fixes evaluated after the fresh full-panel review:

- The unintended root `./-` validation-output artifact was verified as an untracked, non-symlink regular file with exactly the known 50 bytes, then removed. No ignore rule was added.
- Supervisor-executing inline test doubles now return real completed Futures and capture worker `BaseException` values rather than synchronously misreporting them as submission failures. Submission-rejection mocks and real-executor race tests remain distinct and unchanged; the synchronous helper does not establish callback-outside-lock behavior.
- The existing ordinary terminal-stage fault branch attempts one fixed-label, agent-ID, exception-class-only diagnostic before re-raising. No raw exception text or traceback is emitted. Diagnostic failure cannot replace the already-propagating primary; this is not universal background-error monitoring.

The incremental implementation and report are under `.superpowers/sdd/panel-final-warnings/`. The helper's first red was an import-collection failure; its semantic comparison with the old inline callable was post-hoc, not prospective TDD evidence. The diagnostic had three failing semantic tests before its production correction. The writer's 545 affected tests were followed by independent Oracle review: PASS, no mandatory fixes, with 123 independently passing tests. Existing terminal-stage regressions supplement the new tests with unchanged signal, completion, finish and finalization counts.

Fresh final validation ran after all code/test writers and review finished, with the existing project environment and no new exclusions or suppressions:

| Command | Actual exit | Result |
| --- | --- | --- |
| `pytest tests/test_inline_future_double.py tests/test_sub_agent_supervisor.py tests/test_running_agent_visibility.py tests/test_execution_plan.py tests/test_agent_runtime_characterization.py tests/test_graph_memory.py tests/test_graph_memory_integration.py tests/test_graph_memory_backfill.py tests/test_main.py tests/test_trusted_scheduled_spawn.py tests/test_scheduler_failure_classification.py tests/test_terminal_failure_outcomes.py tests/test_subagent_context_persistence.py tests/test_headless_llm_recovery_real_runner.py tests/test_context_payload.py tests/test_p2_longterm_consolidation.py -v --tb=short` | 0 | 790 passed |
| `ruff check .` | 0 | All checks passed |
| `vulture . vulture_whitelist.py --min-confidence 80` | 0 | No output |
| `make check` | 0 | 6458 passed, 2 skipped, 121.30 seconds |
| `openspec validate runner-ownership-and-wal-safety --type change --strict` | 0 | Change is valid |
| `git diff --check` | 0 | No whitespace errors |

The only skips remain `tests/test_consent_scope_wording.py::test_vision_query_scope_line` and `tests/test_path_policy.py::TestIsContained::test_normcase_case_insensitive`; there are no new skips. Actual-exit logs and the final report are in `.superpowers/sdd/panel-final-warnings/final-*`. Tasks 6.2–6.4 were completed after inspecting these results. Only task/evidence prose changed after this gate, not code or tests.

The artifact is absent, ignore rules unchanged and index empty. Branch and `main` remain at planning commit `6f262eb`; implementation and evidence remain uncommitted. No implementation commit, merge, push, separate OpenSpec verification stage or archive was performed.

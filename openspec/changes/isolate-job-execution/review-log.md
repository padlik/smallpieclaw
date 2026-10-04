## proposal Round 1 — 2026-09-30

### 🔴 Fixed
- The existing job log truncates results to 2,000 characters; proposal now explicitly scopes full-result storage within existing bounded retention and defines expired details as unavailable.
- Scheduled runs are sub-agents under the current supervision spec; proposal now expressly modifies that capability to exclude scheduled runs from shared approvals and prompts.

### 🟡 Addressed
- Expandable scheduled output is identified as a deliberate reversal of current plain text; silent delivery is identified as new behavior.
- Delivery failure/restart remains best-effort, with retained job history authoritative.
- Parallel overlap with preserved context remains compatible, with lost-update risk noted.
- `run_now` remains asynchronous and its `notify`/`show` wording is carried forward.
- Turn-end rejection when owned children remain is delegated to the design and specs.

### 🔴 Outstanding
- None after edits; awaiting reviewer confirmation.

## proposal Round 2 — 2026-09-30

### 🔴 Fixed
- Reviewer confirmed both Round 1 contract issues resolved.

### 🟡 Addressed
- Failure notices also follow quiet idle delivery while preserving error classification and next-run content; `llm-error-recovery` is now an explicitly modified capability.

### 🔴 Outstanding
- None; awaiting re-review of the new scope clarification before freeze.

## proposal Round 3 — 2026-09-30

### 🔴 Fixed
- Reviewer confirmed the failure-notification scope clarification and all earlier findings are resolved; `proposal.md` is frozen.

### 🟡 Addressed
- Spec drafting must distinguish silent Telegram delivery (no sound) from failure suppression (which remains prohibited).

### 🔴 Outstanding
- None.

## proposal Decision-level clarification — 2026-09-30

The architecture check surfaced ambiguous failure-notification wording after proposal freeze. The operator confirmed that `notify=false` suppresses both success and failure run-result notices, while existing launch-failure alerts remain. Proposal was reopened before design/specs/tasks existed; its delivery and `llm-error-recovery` wording and the exploration brief were updated. Re-review required before re-freeze.

## proposal Round 4 — 2026-09-30

### 🔴 Fixed
- Reviewer confirmed the amended `notify=false`/launch-failure distinction and all other proposal commitments. Proposal re-frozen.

### 🟡 Addressed
- Delta specs must change the unconditional scheduled-failure Telegram-notify language in `llm-error-recovery` without dropping classification, retained records, or launch-failure alerts.

### 🔴 Outstanding
- None.

## proposal Decision-level expansion — 2026-10-01

The operator approved read-only `/jobs history <tag>` and `/jobs result <execution-id>` so `notify=false` results can be fetched without an agent turn. Proposal and dependent design were reopened; proposal now lists `telegram-command-surface` as modified. Re-review this batch before revising design, ADR, specs, or tasks.

## proposal Round 5 — 2026-10-01

### 🔴 Fixed
- Reviewer confirmed the read-only `/jobs` additions, their new capability mapping, and all prior frozen decisions; proposal re-frozen.

### 🟡 Addressed
- Design/spec drafting must specify run-ID and legacy-entry behavior, tag reuse, long-result transport, missing-argument/unknown-result errors, and documentation for new help entries. No proposal change needed.

### 🔴 Outstanding
- None.

## design Round 1 — 2026-10-01

### 🔴 Fixed
- Shared executor memory and graph-memory tools could still reach operator stores: design now denies all scheduled `memory_write` and `memory_graph_*` operations and removes the proposed new job KV store.
- Proposal did not extend quiet idle treatment to launch failures, reminders, or long-running warnings: design now preserves their existing delivery paths.
- Overly broad result/cancellation narrowing conflicted with council control: design preserves model-facing cancellation for managed sources while limiting only result retrieval to the caller's own spawned children.

### 🟡 Addressed
- Legacy direct-agent scheduler fallback fails closed for agentic tasks; launch failures receive records/IDs, skipped triggers do not.
- Legacy `success` booleans display as partial done/failed; expiry and reused tag behavior are explicit.
- `schedule show.is_running` remains true until every parallel run ends; Telegram character limit is evaluated after entity parsing.
- Removed vague job-log secret-redaction promise and clarified removed once-job history handling.

### 🔴 Outstanding
- None after edits; awaiting reviewer confirmation.

## design Round 2 — 2026-10-01

### 🔴 Fixed
- Reviewer confirmed all three Round 1 issues resolved: scheduled memory tools denied, non-result alerts unchanged, and cancellation compatibility retained.

### 🟡 Addressed
- Cancelled run-result notices follow the same idle queue except existing timeout-cancel suppression; empty-task reminders have no run ID.
- Operator prompt history removal, graph-memory nulls, and non-owned result-tool responses are explicit.
- Log tools remain a distinct default-available observability surface; failed history writes set an actionable job status and truthful notice.

### 🔴 Outstanding
- None; awaiting re-review of the final design clarifications before freeze.

## design Round 3 — 2026-10-01

### 🔴 Fixed
- Reviewer confirmed all design clarifications compatible with frozen proposal and canonical requirements; `design.md` is frozen.

### 🟡 Addressed
- Spec drafting must keep unknown-ID result errors compatible with council control and cover persistence-error precedence and delayed scheduled cancellation notice.

### 🔴 Outstanding
- None.

## adr Round 1 — 2026-10-01

### 🔴 Fixed
- Root-level glossary companion looked like a duplicate numbered ADR-0033; removed it and placed ADR term references in the change-local ADR glossary reference instead.

### 🟡 Addressed
- ADR states its compatibility/amendment relationship to ADR-0005 and ADR-0027, including model-facing result vs cancellation scope.
- Manifest corrects ADR-0001 -> ADR-0002 -> ADR-0003 supersession and names ADR-0016's remaining in-force scope.

### 🔴 Outstanding
- None after edits; awaiting reviewer confirmation.

## adr Round 2 — 2026-10-01

### 🔴 Fixed
- Reviewer confirmed the sole numbered ADR-0033, correct manifest, in-force supersession graph, and compatibility with ADR-0005/0027; ADR batch frozen.

### 🟡 Addressed
- Two cosmetic wording/paragraph notes are not decision-level and require no further changes.

### 🔴 Outstanding
- None.

## specs Round 1 — 2026-10-01

### 🔴 Fixed
- None yet. `openspec validate isolate-job-execution --strict` passed after restoring canonical scenario titles, but semantic review found two archival conflicts.

### 🟡 Addressed
- Reviewer identified configurable retention defaults, persistence-error precedence, completed-unretrieved child finish, prompt-registry owner data, command help and unknown/empty examples, explicit fallback record and parallel risk, and glossary-reference formatting for this batch to resolve.

### 🔴 Outstanding
- S-01: `llm-error-recovery` MODIFIED requirement drops canonical failure detection, failure-notice header, and success non-classification.
- S-02: Three unchanged canonical `sub-agent-supervision` requirements still permit non-owned result retrieval or imply old notification behavior; modify whole requirements with their existing scenarios. The prompt-registry admission-source requirement also needs updating.

## specs Round 2 — 2026-10-01

### 🔴 Fixed
- S-01: Failure detection, original error header and success non-classification restored in `llm-error-recovery`.
- S-02 partly: Additional supervision requirements and prompt-registry admission source now reflect owner-specific behavior.

### 🟡 Addressed
- Reviewer requested precise timeout-cancellation, history-write error precedence, configurable retention, duplicate-scenario cleanup, scheduled notification cross-reference, fallback record, council-control error and glossary formatting.

### 🔴 Outstanding
- S-02 remains at `sub-agent-supervision`'s copied "Supervision compatibility remains stable" scenario, which still permits joining scheduled runs through `get_agent_result`.

## specs Round 3 — 2026-10-01

### 🔴 Fixed
- S-02 closed: owning-turn child result retrieval and scheduled callback delivery are distinct; all modified canonical scenarios are preserved without contradictory scheduled joins.
- `openspec validate isolate-job-execution --strict` passed after the Round 2 corrections; six capability delta specs are frozen.

### 🟡 Addressed
- Timeout, fallback-record, and council-control cross-reference notes are wording-level and do not change implementation decisions; the frozen design and other spec requirements already define those behaviors.

### 🔴 Outstanding
- None.

## tasks Round 1 — 2026-10-01

### 🔴 Fixed
- None yet; structure, file paths, validation commands, and planning-only boundaries passed review.

### 🟡 Addressed
- Reviewer identified overlarge task bundles, missing `main.py` history wiring and adjusted test targets, scheduled terminal-notice edge cases, confirmation dispatch paths, help/architecture documentation, and glossary usage for this batch to resolve.

### 🔴 Outstanding
- T-01: no explicit test/implementation tasks prevent scheduled launches from acquiring the operator's automatic parent summary or `context_payload`.
- T-02: no explicit test/implementation tasks suppress on-demand child completion notifications and automatic ResultsMemory publication.

## tasks Round 2 — 2026-10-01

### 🔴 Fixed
- T-01 and T-02 closed: test-first scheduled parent-summary suppression and on-demand completion/recall suppression are now in the checklist.
- Overlarge work bundles were split and the main.py operator-history wiring and dedicated Telegram idle queue test are tracked.

### 🟡 Addressed
- Reviewer requested supervisor notice handoff, `/help` static checks, existing `/jobs` regressions, token/grant cleanup timing, conditional MCP denial evidence, and documentation/task-size refinements.

### 🔴 Outstanding
- T-03: no test/implementation pair after run-ID storage for failed spawn/fail-closed fallback records with admitted ID and error, or for no records on overlap skips and empty-task reminders.

## tasks Round 3 — 2026-10-01

### 🔴 Fixed
- T-03 closed: separate tests and implementation for failed-spawn/fail-closed records with admitted IDs and no records for skips/reminders follow run-ID storage.
- Reviewer confirmed dependency order, sequential checkbox numbering, tracked test/implementation coverage, and strict change validation; `tasks.md` is frozen.

### 🟡 Addressed
- Optional implementation-time notes: if MCP confirmation is present, route its denial through the existing scheduled policy; split the idle-queue handoff task if it exceeds two hours; add targeted secret/grant-veto tests and update architecture notes in AGENTS.md if the resulting contract changes those entries.

### 🔴 Outstanding
- None.
## design Reopened Round 1 — 2026-10-01

### 🔴 Outstanding
- The design goals still describe every cancellation interface as unchanged; distinguish model-facing `cancel_agent` from operator `/agents` and `/stop`.
- Canonical `sub-agent-council-control` also defines cancellation scope. The operator approved reopening the proposal to declare that capability and adding a corresponding delta spec before freezing the revised design.

### 🟡 Addressed in follow-up
- Clarify the `managed`/`all` model aliases, completed-child accounting, cancellation-response non-disclosure, and the operator-only route for stopping scheduled work.
## proposal Reopened Round 1 — 2026-10-01

### 🔴 Outstanding
- The newly declared `sub-agent-council-control` modification names model cancellation but omits that `wait_for_any_agent` becomes owning-turn-only; expand its capability description.

### 🟡 Addressed in follow-up
- Keep operator `/agents` and `/stop` as preserved context, call scheduled work a **scheduled run**, clarify `managed`/`all` model aliases and the model-facing compatibility impact, and retain existing glossary terminology.
## proposal Reopened Round 2 — 2026-10-01

### 🔴 Fixed
- Reviewer confirmed `sub-agent-council-control` now declares the owning-turn restriction for `wait_for_any_agent` and model-facing cancellation.

### 🟡 Addressed
- Clarified that existing `/agents` cancellation includes, but is not limited to, on-demand and scheduled runs; model-facing `managed`/`all` and compatibility impact remain explicit.

### 🔴 Outstanding
- None; revised proposal frozen before updating design and dependent artifacts.
## design Reopened Round 2 — 2026-10-01

### 🔴 Fixed
- Reviewer confirmed the goals now separate preserved operator cancellation from deliberately narrowed model-facing cancellation, and proposal now declares `sub-agent-council-control`.

### 🟡 Addressed in follow-up
- Clarify managed/all counts, finished-child cancellation and accounting, `wait_for_any_agent` accounting, operator cancellation, resumed-turn behavior, and scheduled capacity risk before freezing the design.

### 🔴 Outstanding
- None in this round; an explicit accounting clarification will receive a further review because it can change implementation behavior.
## design Reopened Round 3 — 2026-10-01

### 🔴 Fixed
- Reviewer confirmed the completed-child discard, current-turn alias counts, operator cancellation accounting, resumed-turn separation, and scheduled capacity trade-off are consistent with the proposal and existing capabilities.

### 🟡 Addressed in follow-up
- Make alias responses unambiguous, distinguish timeout cancellation from a bare wait timeout, define lookup after an owned result is discarded, and require owned-record lookup after registry eviction.

### 🔴 Outstanding
- None; final declarative response-shape clarifications will be rechecked before downstream specs.
## design Reopened Round 4 — 2026-10-01

### 🔴 Fixed
- Reviewer confirmed alias count/non-disclosure, timeout-cancel accounting, discarded-result response, and per-turn owned-record lifetime after registry eviction; revised design frozen.

### 🟡 Addressed downstream
- Specify discarded-ID behavior for `wait_for_any_agent` consistently with `get_agent_result` in the council-control delta; no further design-level decision remains.

### 🔴 Outstanding
- None.
## specs Reopened Round 1 — 2026-10-01

### 🔴 Outstanding
- A MODIFIED council-control requirement replaces its canonical scenarios; reintroduce failed/cancelled own-child completion and cancellation-without-confirmation examples.

### 🟡 Addressed in follow-up
- Cover scheduled-run survival under `managed`/`all`, completed unread children in alias counts, mixed-ID wait failure, and TTL-independent wording; keep user-visible discarded status explicit without introducing an unnecessary glossary term.
## specs Reopened Round 2 — 2026-10-01

### 🔴 Fixed
- Reviewer confirmed the MODIFIED council-control requirements retain failed/cancelled child results and cancellation without an approval prompt; specs batch frozen.

### 🟡 Addressed
- Added mixed-ID whole-call rejection, own-child-only alias counts including discarded completed results, scheduled-run survival, and registry-expiry-independent wording.

### 🔴 Outstanding
- None. Mixed-list error wording can explicitly name inaccessible IDs as a declarative clarification if needed by tests.
## adr Reopened Round 1 — 2026-10-01

### 🔴 Outstanding
- Accepted ADR-0033's new history note points to an original cancellation sentence that was replaced in place; quote the old wording and record the deliberate narrowing decision explicitly.

### 🟡 Addressed in follow-up
- Clarify the unchanged operator-only ADR-0006 capacity cancellation, separate model-facing amendment from unchanged contracts, and note the scheduled capacity trade-off.
## adr Reopened Round 2 — 2026-10-01

### 🔴 Fixed
- Reviewer confirmed ADR-0033 quotes its replaced managed-source assertion and records the deliberate model-facing cancellation narrowing while preserving `/agents` and `/stop`; ADR amendment frozen.

### 🟡 Addressed
- Labelled the dated note an amendment and placed the scheduled capacity limitation in Consequences.

### 🔴 Outstanding
- None.
## tasks Reopened Round 1 — 2026-10-02

### 🔴 Outstanding
- Task 1.7's full scheduled-memory assertion depends on Part 2 tasks 2.1/2.2, and checked task 1.8 still claims to make 1.7 pass. The operator approved only the minimum Part 2 work needed to complete 1.7; preserve the full requirement and keep completion honest.

### 🟡 Addressed in follow-up
- Cover mixed-ID waits, resumed-turn separation, owned-record expiry, operator cancellation, and assign prompt-association rollback to one test/implementation pair.
## tasks Reopened Round 2 — 2026-10-02

### 🔴 Fixed
- Reviewer confirmed 1.7 remains pending real-trigger and runtime-memory proof, while checked 1.8 covers only verified context-payload suppression; the operator authorised 2.1/2.2 as the sole Part 2 dependency. Section 1 task batch frozen.

## Part 1 implementation completion — 2026-10-02

### Completed
- 1.1–1.6, 1.8–1.11 via specialist lanes with focused suites, ruff, and vulture green; 1.11 verified on integrated drain suite (270 passed) plus Telegram progress-dispatch tests.
- 1.12–1.13 implemented directly by the orchestrator after both specialist lanes (senior-dev, fixer) errored on provider usage limits; red phase confirmed (2 failing rollback tests), then green: prompt-registry association failure and worker-pool submit failure now roll back registration, owned-child state, and prompt association (new `remove_sub_agent`). Gates: 44 focused + 351 Part 1 regression passed, ruff + vulture + `git diff --check` clean.

### Open
- 1.7 remains unchecked pending the operator-approved minimal Part 2 work (2.1/2.2) for real cron/once/`run_now` trigger routes and runtime memory-isolation proof.

## Part 1 completion reviews — 2026-10-02

### OpenSpec compliance review (clean: no Critical/High)
- All seven Part 1 fix claims verified in code; 1.1–1.13 and 2.1–2.2 checkboxes honest.
- 🔴 To fix before Part 2: SPEC-01 residual `_current_prompt_id` fallback (`builtin_tools/agents.py:263-266`) vs "MUST NOT infer from mutable ambient executor fields"; DRIFT-01 stale test asserting the old ambient behavior (`tests/test_supervisor_prompt_wiring.py:59-65`).

### Oracle risk review
- 🔴 To fix before Part 2: H1 unbounded drain wait on a non-cooperative child holds the operator lock; M2 legacy direct spawns mint a random owner (never-drained `_owned_children` entry + caller cannot read result — should be unowned legacy); M3 Telegram "Till /reset" in the ~50ms cancel window stores a session-wide grant for a cancelled operation (`telegram_callbacks.py:351-372`).
- 🟡 Deferred minors (recorded, non-blocking): L1 cancel-after-approval micro-race; L2 stale headless result leak; L3 `remove_sub_agent` replay branch; L4 prompt-registry OSError aborting spawn; L5 rollback skips `runner.close()`; L6 wait aborts on first discarded ID; L7 cancel reports success when nothing changed; L8 raw drain markers reach non-Telegram frontends.
- Confirmed OK: turn identity, non-disclosure, ownership locking, scheduled isolation wiring, callback responsiveness, `/stop` accounting. Scheduled memory/confirmation sharing is open scope (tasks 2.3–2.8), not a regression.

### 🔴 Outstanding
- H1, M2, M3, SPEC-01, DRIFT-01 fixes approved by the operator as Part 1 follow-ups; Part 2 proper (2.3+) remains paused pending explicit approval.

## Part 1 review fixes — 2026-10-02

### 🔴 Fixed
- **M2**: on-demand spawns with no turn ID and no trace ID are now unowned legacy (no `_owned_children` entry; result readable by an ownerless caller; random-owner minting removed; stale `test_empty_trace_spawns_receive_distinct_nonempty_turn_owners` replaced with `test_spawn_without_turn_identity_is_unowned`).
- **M3**: `signal_headless_confirmation` returns False for an already-cancelled waiter; Telegram grant recording moved into an `on_approved` callback that runs only for a live waiter — "Till /reset" after cancel stores no session grant and no write executes.
- **H1**: `drain_owned_children` bounded at `OWNED_CHILD_DRAIN_TIMEOUT_S=180s` (120s headless wait + margin); expiry reports the stuck child via the existing `__DRAIN_ERROR__` channel, keeps the entry with `drain_expired=True` (no repeat waits), and late self-cleanup removes it.
- **SPEC-01**: `_current_prompt_id` ambient fallback removed; only the explicit prompt ID is used.
- **DRIFT-01**: stale ambient-behavior test rewritten to assert the explicit-prompt contract (`test_spawn_without_explicit_prompt_ignores_ambient_prompt`).

### 🟡 Accepted residuals (recorded)
- M3 micro-window: a grant can be recorded if cancel lands microseconds after a live waiter accepts; the operation still never executes (post-wake cancel check). Left as-is per design intent.
- L1–L8 deferred minors from the oracle review remain recorded above; L1's micro-race is documented rather than spec-claimed.

### Verification
- Gate suite 418 passed; full suite **2465 passed, 2 skipped** (baseline-matching skips); ruff + vulture + `git diff --check` clean.

### 🔴 Outstanding
- None for Part 1. Part 2 proper (2.3–2.12) and Parts 3–5 remain paused pending operator approval.

## Part 3 completion reviews — 2026-10-03

### Reviewer findings (compliance `ope-2` + risk `ora-3`)
- 🔴 To fix before merge: **CHANGE-01/H1** — a spawn that raises (pool.submit RuntimeError, non-ValueError factory error) is never released or recorded: tag stuck `is_running=true`, skip-policy jobs dead until restart; violates the failed-launch record contract. **STATE-01/M1** — admission split across two lock sections; a parallel run's registration can race a concurrent finish, leaving `is_running=false` while a run is active.
- 🟡 Should fix: **M2** — record()/marker set-clear not atomic (parallel lost-write can be silently cleared); **M3** — `_load()` OSError treated as empty log; a transient read error plus rewrite wipes all history.
- 🟡 Deferred/observations: L1 no fsync before os.replace; L2 `format_for_prompt` lacks expiry limits (resolved by pending 2.11–2.12); L3 large-output cost measurement noted by implementer (~44ms full log, kept JSONL rewrite); L4 retention boundary notes; L5 `next_run` can serialize as string "None"; marker-vs-real-error precedence reading to confirm.
- Confirmed clean: run-ID minting, complete untruncated results, fail-closed fallback (no operator-agent invocation), legacy/partial honesty, read-time expiry, marker overwrite/recovery, per-run release idempotence, run_now async, show contract; test honesty verified incl. real red-phase evidence.

### 🔴 Outstanding
- Operator-approved fix round: CHANGE-01/H1, STATE-01/M1, M2, M3 (M2/M3 via minimal atomic locking), L5 cosmetic. Then checkpoint commit, then pause before Part 2 proper/Part 4.

## Part 3 review fixes — 2026-10-03

### 🔴 Fixed
- **CHANGE-01/H1**: spawn exceptions caught in `_run_via_spawn_agent`, handled as failed launch (record + alert + own-run release); skip-policy jobs recover.
- **STATE-01/M1**: overlap check, run-ID mint, `_running_jobs.add`, and run registration now one locked block; deterministic interleave test proves `is_running` stays true while a run is active.
- **M2**: `_history_lock` covers record-plus-marker set/clear; `_set_job_last_error` serialized under `_running_lock`→`_save_lock`; concurrent failed write's marker survives an earlier success's recovery; no mid-save dict mutation.
- **M3**: `_read_entries()` distinguishes missing file (empty) from read error (None); read error skips rewrite and returns False so the marker fires; existing file bytes untouched (chmod-000 test, root-skipped).
- **L5**: `_next_run_label()` returns "unknown" instead of string "None".

### Verification
- Focused suites green; full suite **2519 passed, 2 skipped** (baseline); ruff + vulture + `git diff --check` clean. Reviewer-flagged test-honesty issues fixed by the implementer before coding.

### 🟡 Deferred/observations (recorded, non-blocking)
- L1 no fsync before os.replace; L2 `format_for_prompt` expiry inconsistency resolves with pending 2.11–2.12; L3 full-JSONL rewrite cost measured acceptable (~44ms); L4 retention boundary/clock-skew notes; marker-vs-real-error precedence reading noted.

### 🔴 Outstanding
- None for Part 3. Awaiting operator approval for the Part 3 checkpoint commit and for Part 2 proper/Part 4.

## Part 2 completion reviews — 2026-10-04

### Reviewer findings (compliance `ope-3` + risk `ora-4`)
- 🔴 To fix: **REQ-01/H1** — scheduled runs can emit the `plan` action (`react_loop.py:1222`, advertised in the full system prompt); `PlanExecutor._get_factory` reads the shared `_sub_agent_factory` through `ScheduledToolDispatch.__getattr__`, so plan steps and diagnostics are built as `PLAN_STEP_AGENT` with the plain executor, operator base memory, ResultsMemory, and the headless confirmation bridge — a complete scheduled-policy escape. Tasks 2.4/2.5/2.7 contracts hold only on direct dispatch routes until this closes.
- 🟡 Cleanup: **DRIFT-01** — dormant `job_history_fn`/`job_history_section` plumbing (ReactContext, ControllerDeps, AgentController, prompt_loader, prompt_builder, `05-response-format.md` slot); behavior requirement met; remove before archive.
- 🟡 Out of scope per design decision 2's own text: **M1** — `log_query`/`log_detail` remain a shared observability surface; the design explicitly makes no new isolation claim about them. Recorded, no fix.
- 🟡 Accepted observations: **L1** dangerous shell in scheduled runs hits the depth-1 block first (generic headless error, by design); **L2** wrapper immutability is by convention (`__setattr__` refusal), no production path writes it.
- Confirmed clean: ScheduledToolDispatch propagation and immutability; memory guards before any store access; classification-before-grant preserved on every scheduled short-circuit (`_gate`, `file_patch`, `file_diff`, vision); depth-1 shell block and shell/secret vetoes unchanged; no MCP confirmation invented; history wiring removed with `format_for_prompt()` contract intact; no on-demand regression; no new shared mutable state.

### 🔴 Outstanding
- Operator-approved Part 2 cycle fix round: refuse the `plan` action for scheduled origin (design non-goal preserves plan-step ownership, so the hard refusal is the minimal spec-compliant fix), plus DRIFT-01 plumbing removal. Then report Part 2 results and wait for operator feedback.

## Part 2 review fixes — 2026-10-04

### 🔴 Fixed
- **REQ-01/H1 (plan escape)**: `_dispatch_action` refuses the `plan` action when the executor is scheduled ("plan is not available for scheduled jobs. Run the steps directly as individual tool calls"); covers both JSON-action and native tool-call routes; PlanExecutor never constructed, no factory called, operator memory untouched. On-demand `plan` regression guard passes. Plan advertisement left in the shared prompt — refusal is the fix; origin-aware templating judged non-trivial and unnecessary.
- **DRIFT-01**: dormant `job_history_fn`/`job_history_section` plumbing fully removed (ReactContext, ControllerDeps, AgentController, prompt_loader, prompt_builder, `05-response-format.md` slot; template frontmatter `variables: []`). `format_for_prompt()` in scheduler.py untouched with its tested contract.

### Verification
- Red phases: 3 plan-refusal failures + reviewers' repro (scheduled→plan→memory_write reached PLAN_STEP_AGENT) confirmed; 5 plumbing-removal failures confirmed. Green: six-file gate 322 passed; focused re-verification 181 passed; full suite **2563 passed, 2 skipped** (baseline); ruff + vulture + `git diff --check` clean.

### 🟡 Recorded, no fix
- **M1**: `log_query`/`log_detail` remain shared observability; design decision 2 explicitly makes no new isolation claim about them.
- **L1**: dangerous scheduled shell commands hit the depth-1 block first (generic headless error) — by design.
- **L2**: `ScheduledToolDispatch` immutability is by convention (`__setattr__` refusal); no production path writes it.

### 🔴 Outstanding
- None for Part 2. Awaiting operator feedback; Part 4 and Part 5 remain paused.

## Part 4 completion reviews — 2026-10-05

### Reviewer findings (compliance `ope-4` + risk `ora-5`)
- 🔴 To fix: **REQ-01** (Medium) — on a failed history write, the "not retained" notice is the only notice and drops the run outcome (no error type/next-run/⚠️ header per llm-error-recovery). **ORA-1** (Medium) — startup-enqueued notices never flush until an unrelated turn ends. **ORA-2** (Medium) — /jobs history/result and Details read the JSONL under a threading lock inside async handlers, blocking the Telegram event loop. **ORA-3** (Medium) — a raising notice sink escapes `_fail_launch` before the direct alert and run release (skip-policy jobs stuck). **ERROR-01** (Low) — "not retained" notice ignores timeout-cancel suppression and launch-failure direct-alert rule; `_unretained_run_ids` never cleaned.
- 🟡 Address together: **DOC-01** — stale README line "written to long-term memory" contradicts new behavior; **LIFECYCLE-01** — /help lacks the deferred-notice wording (README covers it; add to help for task-text honesty); **ORA-4/AMBIGUITY-01** — compact fallback and non-BMP length unit not bounded (bound summary fields; note UTF-16 unit ambiguity); **AMBIGUITY-02** — Details text path sends raw result without header (align with document path or leave; prefer consistency).
- Confirmed clean: failure classification, idle/typing deferral, soundless one-message delivery, notify=false, direct alert/reminder/warning paths, supervisor-never-sends, /stop responsiveness, restart semantics, presentation shapes, Details auth/guards (real execution_log wiring fixed pre-review), /jobs read-only + unknown/empty/legacy honesty, checkbox honesty (4.15 modulo LIFECYCLE-01).

### 🔴 Outstanding
- Operator-approved Part 4 fix round: REQ-01, ORA-1, ORA-2, ORA-3, ERROR-01, DOC-01, LIFECYCLE-01, ORA-4. Then report Part 4 results and await operator feedback before the checkpoint commit and Part 5.

## Part 4 review fixes — 2026-10-05

### 🔴 Fixed
- **REQ-01**: not-retained notices now carry the real outcome (⚠️ header + error type + next run for failures; ✅ summary for success) plus the truthful "Details are unavailable" line, no Details button; notice built in the completion handler so supervisor notify/timeout-cancel gating applies.
- **ORA-1**: `_post_init` schedules a startup flush that waits out any active turn.
- **ORA-2**: history/result/Details lookups run via `asyncio.to_thread`; sends stay on the loop.
- **ORA-3**: `_fail_launch` releases the run in `finally`; sink/notify exceptions isolated — direct alert still sent, skip-policy retriggers work.
- **ERROR-01**: timeout-cancelled + failed write queues no notice; launch failure with failed write sends only the direct alert; `_unretained_run_ids` cleaned.
- **ORA-4/AMBIGUITY-01**: `bound_display_text` (tags ellipsized at 100 chars) + `ensure_notice_fits` re-validates rendered length pre-send; code-point vs UTF-16 unit note added.
- **DOC-01**: stale README "long-term memory" line rewritten to actual behavior.
- **LIFECYCLE-01**: /help now mentions deferred silent notices.
- **AMBIGUITY-02**: ruled raw text stays on the short-result path (spec: "one complete retained outcome"; existing tests pin it); document path keeps its header. Ruling recorded: consistent-with-tests reading; costs nothing if a future spec wants headers on short results.

### Verification
- Red phases confirmed per fix; 7-file gate 395 passed; full suite **2746 passed, 2 skipped** (baseline); ruff + vulture + `git diff --check` clean. One flaky supervisor test observed once during a broken-fixture window; passed in isolation, three reruns, and every later run — no action.

### 🔴 Outstanding
- None for Part 4. Awaiting operator feedback on the Part 4 report; Part 5 remains paused.

### 🔴 Outstanding
- None.

### 🟡 Addressed
- Mixed-ID and terminal-result regressions, resumed-turn separation, owned-record expiry, operator cancellation accounting, and prompt/submission rollback each have a single test/implementation owner.

### 🔴 Outstanding
- None.

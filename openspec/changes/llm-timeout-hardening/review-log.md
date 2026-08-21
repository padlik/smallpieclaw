## proposal Round 1 — 2026-08-21

### 🔴 Fixed
- (none this round — review only; no fixes applied yet)

### 🟡 Addressed
- (none this round — review only)

### 🔴 Outstanding
- C1 (critical): Scheduled-job failure notification (What Changes #6, Impact scheduler.py) has no capability home. New capability `llm-error-recovery` is scoped to "interactive runs", excluding scheduled jobs, and no modified capability covers the scheduler. Assign it a capability before generating specs.
- H1: Contradiction — proposal says checkpoint is deleted "on cancel", but brief commits that a 120s prompt timeout (treated as cancel for the agent thread) must NOT delete the checkpoint. Preserve the distinction.
- H2: Startup-scan notification ("💾 Found unfinished run… send /resume") from brief is missing from What Changes and Impact (no startup module listed).
- H3: Retryable semantics not reflected — brief marks `context` and `permanent` as non-retryable; proposal shows Retry+Cancel unconditionally and omits the 7th "unknown/other LLMError" classification.
- H4: Checkpoint contents under-specified — proposal lists (messages, step, goal_idx, max_steps) but omits brief-committed fields json_fail_streak, model (resume correctness) and user_goal, created_at, error_info (listing/card).
- H5: Error card "count of preserved tool results" (brief open-Q5, resolved yes) is missing.
- H6: /resume edge cases (busy agent, corrupted checkpoint, no-checkpoints message, new-message deferral, sub-agent exclusion) not surfaced; carry into specs.

### Notes
- Structure valid (Why/What Changes/Capabilities/Impact). Capability names are correct kebab-case.
- Modified capabilities verified against openspec/specs/: telegram-command-surface, telegram-progress-panel, prompt-tracking all exist.
- No-provider-changes constraint and config defaults (retry_timeout_seconds=120, checkpoint_enabled=true) match the brief.

## proposal Round 2 — 2026-08-21

### 🔴 Fixed
- C1: Scheduled-job failure notification now has a capability home — `llm-error-recovery` rescoped to cover both interactive runs and scheduled jobs, with scheduled-job notification named explicitly (proposal.md:19).
- H1: Timeout-vs-cancel deletion contradiction resolved — checkpoint deleted only on successful completion or explicit Cancel; 120s timeout preserves the checkpoint for /resume (proposal.md:9).
- H2: Startup checkpoint scan added as its own What Changes bullet with main.py in Impact (proposal.md:11, :32).
- H3: All seven classification types enumerated including `unknown`; non-retryable types (context, permanent) suppress Retry and show only Cancel (proposal.md:7–8).
- H4: Checkpoint field set fully specified — trace_id, user_goal, messages, step, goal_idx, max_steps, json_fail_streak, model, created_at, error_info (proposal.md:9).
- H5: Error card now shows count of preserved tool results (proposal.md:8).
- H6: /resume edge cases surfaced — busy agent, corrupted checkpoint, no-checkpoints, deferred new message, sub-agent exclusion (proposal.md:10).

### 🟡 Addressed
- What Changes and Impact are now fully consistent; Impact adds main.py for the startup scan.
- Error-card contents (type, model, step/max-steps, preserved-results count) now match the brief's checkpoint contract and open-Q5.

### 🔴 Outstanding
- none (blocking). Proposal is ready to freeze.

### 💡 Optional (defer to specs, non-blocking)
- Non-retryable checkpoint lifecycle: non-retryable errors (context, permanent) still write a checkpoint but expose only Cancel. If the card times out, /resume could re-attempt a doomed run. Specs should decide whether to skip checkpoint writes for non-retryable types or have /resume refuse them.

## design Round 1 — 2026-08-21

### 🔴 Fixed
- (none this round — review only; no fixes applied yet)

### 🟡 Addressed
- (none this round — review only)

### 🔴 Outstanding
- none (blocking). No contradictions with the frozen proposal, no provider scope creep, no superseded ADRs. Design is substantively ready to freeze; items below are non-blocking should-fixes.

### 🟡 Should Fix (before freeze — declarative, no proposal re-open)
- D1a: `context` type has no traceable source exception. Design Context lists `LLMError → LLMPermanentError, LLMEmptyResponseError, LLMCancelledError` — omits brief-committed `LLMContextOverflowError` (source of `context`) and adds `LLMCancelledError`, which maps to no classification row. Name `LLMContextOverflowError` for `context`; reconcile `LLMCancelledError`.
- D1b: Full 7-row classification table (exception→type→message→retryable) from the brief is not reproduced in D1 — only the `timeout` example appears in the JSON. Reproduce it in D1 or explicitly defer to specs.
- D3a: Error card "count of preserved tool results" (frozen proposal.md:8, brief open-Q5) is absent from design's card-content Open Question. Add the count.
- D5a: trace_id continuity on resume unspecified. Both flows call delete(trace_id) on success; design must state the resumed run reuses the checkpoint's stored trace_id (else delete targets wrong file → leak, and log correlation breaks).
- L167: ADR-0007 "extended with a new parameter" is imprecise and reads as scope creep — the real change is `agent_controller.py` run(resume_from) (in frozen Impact); `agent_runtime.py` is not. Reword to reference agent_controller.py.

### 💡 Optional (defer, non-blocking)
- Describe checkpoint_enabled=false degradation (inline retry can still work in-memory; only /resume/crash-recovery lost).
- Migration #4 rollback wording is muddled ("reverts to default = new behavior"); note there is no full feature kill-switch by design.

### Notes
- Template complete: Context, Goals/Non-Goals, Decisions (rationale + alternatives), Risks, Migration, Open Questions all present.
- All brief Final-Approach sections map to D1–D7; all three cross-module data flows present; all five brief Open Questions resolved.
- Round-2 optional item (non-retryable checkpoint lifecycle) resolved in D3/D4/Risks.
- ADRs in-force: ADR-0014 (pattern only), ADR-0007 (not modified). No superseded ADRs referenced.

## design Round 2 — 2026-08-21

### 🔴 Fixed
- D1a: `context` type now has a traceable source exception. Context (design.md:11) lists `LLMContextOverflowError` in the `LLMError` hierarchy and reconciles `LLMCancelledError` as a separate `RuntimeError` subclass that propagates immediately and is never classified. D1 rationale (:39) and table (:49) both name `LLMContextOverflowError → context (non-retryable)`.
- D1b: Full 7-row classification table (exception→type→message→retryable) reproduced in D1 (design.md:43–51), matching the brief exactly.
- D3a: Error card content now explicitly includes "count of preserved tool results" (design.md:94), consistent with frozen proposal.md:8 and brief open-Q5. Card also lists type+message, model, step/max-steps, truncated detail.
- D5a: trace_id continuity specified — resumed run reuses the checkpoint's stored trace_id for log correlation and correct delete-on-success (design.md:136).
- L167: ADR-0007 scope-creep wording removed — Open Questions (design.md:179) now frames the change as an `agent_controller.py` run(resume_from) extension (in frozen Impact) and explicitly states no `agent_runtime.py`/ADR-0007 modification.

### 🟡 Addressed
- All five Round 1 🟡 should-fix items resolved with no new issues introduced. Classification table, card contents, and exception hierarchy are now mutually consistent and match both the brief and the frozen proposal.

### 🔴 Outstanding
- none. Design is consistent with the frozen proposal, template-complete, references only in-force ADRs, and all Round 1 findings are closed. Ready to freeze.

### 💡 Optional (carry to specs, non-blocking — unchanged from Round 1)
- checkpoint_enabled=false degradation path still not described (inline retry can work in-memory; only /resume/crash-recovery lost).
- Migration #4 rollback wording still muddled ("reverts to default = new behavior"); note there is no full feature kill-switch by design.

### Notes
- New factual claim to confirm at apply time: `LLMCancelledError` is a `RuntimeError` subclass (not `LLMError`) and is never classified — verify against providers/_errors.py. Non-blocking; does not affect the classification design.

## specs Round 1 — 2026-08-21

### 🔴 Fixed
- (none this round — review only; no fixes applied yet)

### 🟡 Addressed
- (none this round — review only)

### 🔴 Outstanding
- none (blocking). All 4 spec files are consistent with the frozen proposal and design. Correct ADDED/MODIFIED headings; prompt-tracking MODIFIED block copies the full existing requirement and preserves all 6 original scenarios plus 3 new classification scenarios. All 7 error types, checkpoint lifecycle (write-on-error, atomic, delete-on-success/cancel, survive-on-timeout), /resume edge cases, startup scan, scheduled-job notification, and config section all present. Specs are substantively ready to freeze; items below are non-blocking should-fixes.

### 🟡 Should Fix (before freeze — declarative, no proposal/design re-open)
- S1: Classification catch-all too narrow. llm-error-recovery:5 and the `unknown` scenario scope the fallback to "other LLMError subtypes," but _request_turn() catches `except Exception` and raw httpx.HTTPStatusError reaches it (design.md:11). A non-429 HTTPStatusError (500/503) or any non-LLMError exception has no bucket → raw exception dump, the exact bug this change fixes. Widen `unknown` to "any otherwise-unclassified exception (retryable)". Spec-text fix only; design table (429-only) can stay frozen.
- S2: Missing negative scenario for cancellation. design.md:11 mandates LLMCancelledError (RuntimeError, not LLMError) propagates and is never classified. No spec scenario asserts this → a /stop mid-LLM-call could be caught by the catch-all, writing a checkpoint and showing a retry card. Add a scenario: LLMCancelledError → no classification, no checkpoint, no error card. Must be reconciled with S1's widened catch-all (explicitly exclude LLMCancelledError).
- S3: telegram-progress-panel "Error card replaces typing indicator" contradicts itself — title says "replaces" but THEN says "typing indicator continues until the user responds or timeout." Ambiguous UI behavior. Make title and assertion agree (likely intent: indicator persists during the 120s block).

### 💡 Optional (non-blocking)
- rate_limit/empty scenarios don't restate retryable=true the way context/permanent/unknown do; add for symmetry.
- Scheduled-job requirement "matches known error type strings" is loose; name the concrete prefixes (design D6). Scenario already anchors on "❌ LLM error: TimeoutException".

### Notes
- All 4 proposal capabilities have spec files (llm-error-recovery new; telegram-command-surface, telegram-progress-panel, prompt-tracking modified).
- telegram-command-surface and telegram-progress-panel correctly use `## ADDED Requirements` (new requirements on existing capabilities, not modifications) — no full-block copy needed.
- prompt-tracking MODIFIED verified against openspec/specs/prompt-tracking/spec.md: full requirement copied, terminal-status sentence appended, all originals preserved, 3 classification scenarios added. Matches proposal:24 + design D7.
- Checkpoint field set matches design D2; config degradation (checkpoint_enabled=false) and /resume non-retryable refusal close the design's carried-over open items.

## specs Round 2 — 2026-08-21

### 🔴 Fixed
- S1: Classification catch-all widened. llm-error-recovery:5 now defines `unknown` as "any otherwise-unclassified exception, including non-429 HTTPStatusError, generic LLMError, or other exceptions"; the `unknown` scenario matches. Raw-dump gap closed — no exception falls through unclassified.
- S2: Cancellation negative scenario added. Requirement text carves out LLMCancelledError ("SHALL NOT be classified — propagates immediately without checkpoint, error card, or retry prompt"); new scenario "User cancellation is not classified or checkpointed" asserts propagation, no checkpoint, no error card/retry prompt, run terminates as cancelled. Correctly ordered before the S1 catch-all so a /stop mid-call is never swallowed as `unknown`.
- S3: Typing-indicator contradiction fixed. telegram-progress-panel retitled "Error card is sent while typing indicator persists"; THEN now reads "typing indicator continues until the user responds or the retry timeout expires." Title and assertion agree.

### 🟡 Addressed
- All three Round 1 🟡 should-fix items resolved with no new issues introduced. S1 and S2 are mutually consistent (cancellation carve-out precedes the widened catch-all). S1's widening does not conflict with the frozen design's `permanent` row — design maps `permanent` to the wrapped LLMPermanentError, so a raw non-429 HTTPStatusError correctly lands in `unknown`/retryable.

### 🔴 Outstanding
- none. All 4 spec files are consistent with the frozen proposal and design, use correct ADDED/MODIFIED headings, and all Round 1 findings are closed. Specs are ready to freeze.

### Notes
- Round 1 optional items (rate_limit/empty retryable symmetry; scheduled-job "known error type strings" wording) remain non-blocking and were not required for freeze.

## adr Round 1 — 2026-08-21

### 🔴 Fixed
- (none this round — review only; no fixes applied yet)

### 🟡 Addressed
- (none this round — review only)

### 🔴 Outstanding
- none (blocking). ADR-0021 is correctly sequenced (0021 = 0020 + 1), immutable (purely additive; references ADR-0014 as atomic-write pattern but modifies no prior ADR; supersedes nothing), MADR-short compliant (Context/Decision/Consequences), and fully consistent with the frozen design. Manifest lists all 19 in-force ADRs (0001 + 0003–0020; 0002 correctly omitted as superseded). Scoping is correct — only the durable checkpoint persistence layer got an ADR; transient classification/card/UX decisions correctly excluded.

### 🟡 Should Fix (non-blocking — declarative, no upstream re-open)
- A1: ADR did not state how `data/run_checkpoints/` reconciles with in-force ADR-0019 (XDG for all storage paths). Fixed: added one-line note that `data/` is resolved relative to the agent's XDG data directory per ADR-0019, consistent with existing `data/` stores.

### Notes
- Immutability rule satisfied: no prior ADR edited; no status lines changed.
- MADR-short format matches repo house style (Status/Date/Context/Decision/Consequences).
- ADR captures the durable decision only; error-classification (D1) correctly not promoted to an ADR (local ~30-line function, not architecture).

## tasks Round 1 — 2026-08-21

### 🔴 Fixed
- (none this round — review only; no fixes applied yet)

### 🟡 Addressed
- (none this round — review only)

### 🔴 Outstanding
- none (blocking). All D1–D7 decisions, all 7 spec requirements, and every Impact-section file have traceable tasks in dependency-correct order. Checkbox format correct; validation task (13.2 `openspec validate --strict`) present; task sizes within one-session scope. Items below are non-blocking should-fixes.

### 🟡 Should Fix (before freeze — declarative, no upstream re-open)
- T1: No test for the `checkpoint_enabled=false` branch. Spec "LLM error handling configuration" scenario "Checkpoint disabled still allows inline retry" asserts no file written + inline retry works in-memory + /resume finds none. Task 11.4 tests only config parsing; 4.6 tests the enabled path. Add a test task for the disabled branch.
- T2: config example filename mismatch. Task 11.3 / frozen proposal Impact say `config.example.toml`, but repo file is `config.toml.example`. Verify at apply time so 11.3 edits the existing file.
- T3: Typing-indicator persistence (frozen telegram-progress-panel, specs Round 2 S3) has no dedicated task/test. §6 renders the card but nothing asserts the indicator persists through the 120s block. Add an assertion in 6.4.

### 💡 Optional (non-blocking)
- A few spec scenarios lack explicit test lines (startup multiple→most-recent in 8.2; /resume main-agent-only in 7.4; deferred-message-during-prompt). Low risk; add for completeness if desired.
- 6.1 (`_ProgressPanel`) calls `_send_llm_error_prompt` defined on `TelegramInterface` (6.2) — ensure the panel holds the reference.

### Notes
- LLMCancelledError path fully covered: 2.1 exclusion, 4.1 catch-first-and-reraise, 2.2 propagation test, 13.3 hierarchy verification — closes specs S2.
- Widened `unknown` catch-all (specs S1) reflected in 2.1.
- Checkpoint lifecycle (atomic write, delete-on-success/cancel, survive-on-timeout, OSError non-fatal) fully task-covered incl. negative paths (4.6, 1.3).

## tasks Round 2 — 2026-08-21

### 🔴 Fixed
- T1: `checkpoint_enabled=false` branch now tested. New task 4.7 asserts no checkpoint file written + inline retry works from in-memory state + `/resume` reports "No unfinished runs to resume" — matches spec "Checkpoint disabled still allows inline retry".
- T2: config example filename corrected. Task 11.3 now targets `config.toml.example` (the actual repo file), no longer the non-existent `config.example.toml`.
- T3: typing-indicator persistence now tested. Task 6.4 asserts the indicator persists while the error card is shown (until user responds or retry timeout expires) — matches frozen telegram-progress-panel (specs Round 2 S3).

### 🟡 Addressed
- All three Round 1 should-fixes resolved with no new issues. 4.7 appended cleanly (§4 → 4.1–4.7, no renumbering); 6.4 and 11.3 edited in place; §13 validation untouched.

### 🔴 Outstanding
- none. tasks.md is complete (D1–D7, all 7 spec requirements, all Impact files), dependency-ordered, correctly formatted, includes `openspec validate --strict` (13.2), and all Round 1 findings are closed. Ready to freeze.

### Notes
- Round 1 optional items (startup multiple→most-recent test; /resume main-agent-only test; deferred-message-during-prompt test; panel→interface reference) remain non-blocking and were not required for freeze.
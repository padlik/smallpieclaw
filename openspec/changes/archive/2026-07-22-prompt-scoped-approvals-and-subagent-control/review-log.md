## proposal Round 1 — 2026-07-21

### 🔴 Fixed
- (none — reviewer is read-only; nothing auto-fixed this round)

### 🟡 Addressed
- (none — see Outstanding; issues are raised, not yet resolved)

### 🔴 Outstanding

**None are hard blockers to proposal approval.** The proposal is substantively complete
and consistent with explore-brief.md on all 6 problems, all 4 rejected alternatives, the
mapping tables, the data flows, and the 6 resolved questions. The following should be
resolved before or during the specs batch:

🟡 **[Capability mapping] `log_query` prompt_id filter is likely filed under the wrong capability.**
   - proposal.md places "`log_query` gains a `prompt_id` filter" under the
     `structured-event-logging` Modified Capability.
   - `structured-event-logging` is scoped to log *emission/identity* (LogEvent taxonomy,
     `bind_run_context`, dual-sink). The `log_query` self-analysis tool is owned by the
     separate existing capability `runtime-log-introspection`.
   - Risk: the specs batch would write the `log_query prompt_id` delta into the wrong
     spec.md → rework. Recommend splitting: keep "prompt_id bound into structlog context"
     under `structured-event-logging`, move "`log_query` gains prompt_id filter" to a
     Modified `runtime-log-introspection`.

🟡 **[Impact consistency] Supervisor's registry reference is under-specified.**
   - proposal.md says the supervisor "records the spawned agent_id against the active
     prompt via the registry reference on the executor," but the builtin_executor Impact
     entry lists only two new fields: `_prompt_approval_set` and `_current_prompt_id`. There
     is no `PromptRegistry` reference field named.
   - The flow requires the supervisor to reach both the prompt_id AND the registry. Name
     the registry wiring in Impact (a third executor field, or state how the supervisor
     obtains the PromptRegistry) so the specs/design batch is unambiguous.

🟡 **[Completeness] No Non-goals / out-of-scope statement.**
   - The brief's 4th rejected alternative — "main-agent detachment (return to idle while
     sub-agents run)" — was explicitly declared out of scope. The proposal never restates
     this boundary. Without it, an implementer could reasonably read `wait_for_any_agent`
     + `/stop` cascade as an opening to add detachment. Add a one-line Non-goals note.

### 💡 Optional
- proposal.md What Changes omits two approval-scope dimensions from the brief table:
  "exact tool string, no wildcards" and "`shell` always confirmed/blocked, untouched."
  Not contradicted anywhere, so low risk — but stating them would prevent an implementer
  from assuming wildcard matching or that shell could be auto-approved.
- Q2 (poll loop, 200ms) for `wait_for_any_agent` is an implementation detail correctly
  deferred; fine to leave out of proposal, but ensure it lands in design.md.

### ⚖️ Verdict
**Ready to proceed to the next batch (design/specs), conditional on resolving the
`log_query` capability mapping (top 🟡) before the specs delta is written.** The other two
🟡 items are best fixed now but do not block. No decision-level conflicts with the brief.
## proposal Round 2 — 2026-07-21

### 🔴 Fixed
- **[Capability mapping] `log_query` prompt_id filter now correctly owned.** Verified
  `openspec/specs/runtime-log-introspection/spec.md` defines the `log_query` tool. The
  proposal now splits the delta: `structured-event-logging` keeps only "prompt_id bound
  into structlog context," and a new Modified Capability `runtime-log-introspection` owns
  "`log_query` gains a prompt_id filter parameter." Spec-delta target is now correct.

### 🟡 Addressed
- **[Impact consistency] Supervisor's registry reference now named.** `_prompt_registry`
  added as a third new field on `builtin_executor.py`. Finally block clears only
  per-prompt state (`_prompt_approval_set`/`_current_prompt_id`), leaving the long-lived
  `_prompt_registry` reference intact.
- **[Completeness] Non-goals now stated.** Main-agent detachment explicitly out of scope.
- **[Optional → done] Shell approval boundary stated.** `shell` never auto-approved.

### 🔴 Outstanding
- (none)

**Verdict: Batch passes.**

## design Round 2 — 2026-07-21

### 🔴 Fixed
- **[Blocker] Zone/approve-all mechanism now described correctly.** design.md:94 rewritten:
  approve-all "short-circuits the zone-triggered *confirmation* (auto-satisfies it via
  `confirm(token)`), not the zone *classification* itself — `execute()` still runs zone
  classification first and stages out-of-zone/agent-internal ops as `requires_confirmation`
  (`react_loop.py:1275-1288`), then the approve-all check at line 1282 auto-satisfies that
  confirmation." Ordering now matches the code. Behavior expansion explicitly acknowledged.
  Blocker resolved.

### 🟡 Addressed
- **[`_prompt_registry` wiring] Prose contradiction resolved.** design.md:74 and D3 now agree:
  `_prompt_registry` wired once in `main.py` (long-lived), only `_current_prompt_id`/
  `_prompt_approval_set` are per-run. Residual cosmetic: C4 diagram still lists it under
  `run()` — authoritative prose correct, non-blocking.
- **[D8 persistence] `sub_agent_ids` durability decided.** `start()` writes `sub_agent_ids=[]`;
  `add_sub_agent()` appends crash-safe update line; `finish()` appends full list; reload
  replays last-line-wins. Consistent with frozen proposal.
- **[Startup reload] Decided.** D8 resolves in favor of reload (next prompt_id = max + 1)
  so "Prompt #N" stable across restarts.

### 🔴 Outstanding
- (none)

**Verdict: Batch passes.**

## specs Round 1 — 2026-07-21

### 🔴 Fixed
- (n/a — first specs review)

### 🟡 Addressed
- (none auto-resolved — two non-blocking findings folded in before archive)

### 🔴 Outstanding
- (none — batch passes)

### Findings (non-blocking, folded in)
🟡 **[Frozen commitment not pinned] `shell` never-auto-approved boundary has no scenario.**
   Added a scenario to builtin-tool-execution pinning that `shell` cannot enter the shared
   approval set.
🟡 **[Cross-capability duplication] `/prompts` behavior specified in two capabilities.**
   Narrowed telegram-command-surface `/prompts` to discovery/surface; prompt-tracking owns
   the content contract.

### 💡 Optional
- `wait_for_any_agent` empty-`agent_ids` edge case and `cancel_agent` "managed" vs "all"
  ambiguity — noted, low priority.

### ⚖️ Verdict
Batch passes. All 6 capabilities present; MODIFIED blocks are full copies with edits;
committed zone scenario included; prompt_id added correctly; tool count 15→17;
confirmation-capable=6; all scenarios Gherkin-style. No decision-level conflicts.

## adr Round 1 — 2026-07-21

### 🔴 Fixed
- (n/a — first ADR review)

### 🟡 Addressed
- (none)

### 🔴 Outstanding
- (none — batch passes)

### Findings
✅ Manifest lists all 10 in-force ADRs (0001-0010); none superseded.
✅ ADR-0011 meets the bar (durable approval-scope contract change, affects future changes).
✅ Consistent with frozen design (D1, D2, zone-confirmation-not-classification).
✅ ADR-0010 preserved (Supersedes: None).
✅ MADR-minimal style followed (all six sections).
✅ Honest consequences (3 good, 2 bad, 1 neutral — zone expansion honestly logged).
💡 Carry-forward (shell boundary spec scenario) — already folded in during specs Round 1.

**Verdict: Batch passes.**

## tasks Round 2 — 2026-07-21

### 🔴 Fixed
- **[Blocker] PromptRegistry runtime wiring + prompt_id propagation now covered.** New task 1.4
  constructs the PromptRegistry singleton in main.py and wires it onto
  BuiltinExecutor._prompt_registry (long-lived, per design D3). New task 1.5 calls
  PromptRegistry.start() in _run_agent_task_locked before run_in_executor, adds a prompt_id
  parameter to AgentController.run(), and calls finish() in the finally. Task 1.6 tests
  start/finish invocation. Tasks 8.2 and 10.2 now have a real prompt_id source.

### 🟡 Addressed
- **[Shell boundary] Enforced by tasks now.** Task 3.1 restricts the approve-all button to file
  tools only (never shell). Task 3.3 tests that shell can never enter auto_approve_tools.

### 🔴 Outstanding
- (none — batch passes)

**Verdict: Batch passes.**

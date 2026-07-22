## proposal Round 1 — 2026-07-23

This round reviews proposal.md only (first batch). No design.md/specs/tasks.md exist yet.
Baseline: explore-brief.md. Nothing is frozen yet.

### 🔴 Outstanding (blockers — fix before proposal freezes)

- **`agent_runtime.py` dropped from the removal scope** — The brief's "Remove (code paths)" list explicitly names `agent_runtime.py: executor and creator params if present`. They ARE present: `agent_runtime.py:221-222` and `:370-371` pass `executor=…` / `creator=…` when constructing the controller. The proposal's Removed section names only `main.py` and `agent_controller.py`. An implementer working from this proposal will leave `agent_runtime.py` wired to removed constructor params → broken construction path + `test_agent_runtime_skeleton.py` failures. Add `agent_runtime.py` to the Removed/Modified list.

- **Test-suite impact is not in scope (open question #4 unaddressed)** — The brief's open question #4 flags that fixtures and tests referencing the removed surface will break. The proposal only says "AGENTS.md … testing sections updated" — that is docs, not the test files. Since AGENTS.md mandates `make check` (lint+test) before commit, this is the single largest churn area and it is invisible in the proposal. Add an explicit "Tests & fixtures" scope item enumerating the impacted files/kinds so tasks.md can cover them.

### 🟡 Should Fix

- **`/tools` empty-state message (open question #2) unresolved** — Brief open question #2 asks whether the post-removal empty state should stay "No tools registered." or change to something MCP-aware. The proposal confirms `/tools` lists MCP tools but does not decide the message. Resolve it (even "keep as-is") so it isn't silently dropped.

- **`telegram_callbacks.py` missing from the Kept list** — Brief lists it under "Keep as-is"; the proposal's Kept section omits it. It is genuinely unaffected, but the brief called it out, so list it to close the traceability gap.

### ✅ What's Done Well

- Every "Remove entirely (files)" and "Strip to MCP-only" commitment is captured accurately. The `tool_registry.py` strip detail matches the brief exactly — removed set, kept set, and `__init__(self)` empty-registry constructor.
- Open questions #1 (orphaned dirs — Scope), #3 (`/reindex`), and #5 (`patterns.py` entry) are all resolved.
- Adds value beyond the brief: the "Specs Modified" section (native-tool-calling, file-access-zones) and the `add-nsjail-shell-isolation` overlap analysis are correct forward-looking additions with no contradictions.

## proposal Round 2 — 2026-07-23

Re-review of proposal.md against Round 1 findings. Baseline: explore-brief.md.
Nothing frozen yet. Line-number claims verified against source.

### 🔴 Outstanding (blockers)

None. Both Round 1 blockers are resolved.

### 🟡 Should Fix

- **`agent_runtime.py` removal list omits `build_react_context` (lines 221-222)** — Round 1's blocker cited `agent_runtime.py:221-222` and `:370-371`. The Round 2 fix added the constructor params (149-150), attributes (172-173), and SubAgentRunner kwargs (370-371) — but initially omitted the `build_react_context()` site at 221-222. **Fixed in this round**: the proposal now explicitly lists all four sites (149-150, 221-222, 370-371, plus attributes).

### ✅ What's Done Well

- Round 1 blocker #1 line references now verified accurate against source: 149-150 (constructor params), 221-222 (build_react_context), 370-371 (SubAgentRunner kwargs) all confirmed correct.
- The new Tests & Fixtures section closes the largest Round 1 gap and is honest about coverage ("at minimum" hedge on the executor=/creator= list).
- Both Round 1 🟡 items (`/tools` empty-state message, `telegram_callbacks.py` in Kept) resolved cleanly.

### ⚖️ Review Verdict

**Ready to freeze.** All Round 1 blockers and 🟡 items resolved. The `build_react_context` site is now explicitly enumerated.

## design Round 1 — 2026-07-23

This round reviews design.md. proposal.md is FROZEN (baseline). Also referenced: explore-brief.md and the two baseline specs under openspec/specs/. Source line claims in the design were verified against the actual tree.

### 🔴 Critical Issues (implementation blockers)

- **D8 file-access-zones — factual error that leaves a deleted directory referenced in the spec.** design.md asserted *"`tools/` is not currently in the spec's directory list (only `tools_generated/` appears at line 14)."* This is **false**. The live spec `openspec/specs/file-access-zones/spec.md:14` reads: `(`data/`, `tools/`, `tools_generated/`, `skills/`, `prompts/`, log dir, vault dir)` — it contains **both** `tools/` and `tools_generated/`. **Fixed**: D8 now removes **both** `tools/` and `tools_generated/` from the line-14 list.

### 🟡 Should Fix

- **main.py `ToolExecutor`/`ToolCreator` removal absent from design decisions.** D3's site list omitted main.py (the composition root), which imports, constructs, and passes both classes. **Fixed**: D3 now explicitly lists main.py with line references (116-118, 269-270, 325-326).

- **native-tool-calling — "Script tools excluded" scenario becomes obsolete.** D8 missed `openspec/specs/native-tool-calling/spec.md:96-99` ("Script tools excluded"), whose GIVEN is *"script tools (`.sh`/`.py`) exist in the tool registry."* After this change the registry is MCP-only and can never hold script tools. **Fixed**: D8 now explicitly calls out this scenario for removal.

- **`telegram_callbacks.py` dropped from "What is NOT changing."** The frozen proposal's Kept list includes `telegram_callbacks.py`. **Fixed**: added back to the "What is NOT changing" section.

### ✅ What's Done Well

- D1 (ToolRegistry MCP-only) is precise and matches source.
- D3's TypeError argument is sound — removing non-Optional dataclass fields converts missed sites into hard failures.
- D8 native-tool-calling claims are accurate against the baseline spec.
- D6 verified against source: `builtin_tools/patterns.py:28` is the only `tools_generated/` pattern entry.
- Cross-module data-flow diagram matches source. R2 (nsjail overlap) and R3 (orphaned dirs) are consistent with the frozen proposal.

### ⚖️ Review Verdict

**Not ready to freeze until D8 factual error is fixed.** One 🔴 (one-line correction), three 🟡 (all fixed).

## design Round 2 — 2026-07-23

Re-review of design.md against Round 1 findings. proposal.md is FROZEN (baseline); also referenced: explore-brief.md and the two baseline specs. New/changed source line-claims verified against the tree.

### 🔴 Critical Issues (implementation blockers)

None. The Round 1 blocker is resolved.

- **D8 file-access-zones factual error — RESOLVED.** design.md now states the line-14 list "includes both `tools/` and `tools_generated/`" and drops **both**, leaving `data/, skills/, prompts/, log dir, vault dir`. Verified against `openspec/specs/file-access-zones/spec.md:14`.

### 🟡 Should Fix

None outstanding. All three Round 1 🟡 items are resolved:

- **D3 main.py inclusion — RESOLVED.** D3 now lists main.py with imports (116-117), construction (269-270), and AgentController kwargs (325-326), all verified exact against source.
- **"Script tools excluded" scenario — RESOLVED.** D8 now explicitly marks native-tool-calling spec.md:96-99 for removal, with sound reasoning (registry is MCP-only, so the GIVEN precondition is structurally unreachable).
- **telegram_callbacks.py — RESOLVED.** Restored to "What is NOT changing" with the correct rationale.

### ✅ What's Done Well

- D9 (vulture_whitelist.py cleanup) is a good addition — correctly scoped as mechanical, with the `make lint` gate named as the safety net.
- D8's structural-property argument for removing the "Script tools excluded" scenario is the right call.
- The D3 TypeError-as-tripwire argument remains sound and now covers the composition root.

### ⚖️ Review Verdict

**Ready to freeze.** All Round 1 🔴 (1) and 🟡 (3) items are resolved and verified against source. design.md is consistent with the frozen proposal and the explore brief.

## specs Round 1 — 2026-07-23

This round reviews the specs batch: specs/native-tool-calling/spec.md and specs/file-access-zones/spec.md. proposal.md and design.md are FROZEN (baseline). Verified against both frozen artifacts and the two baseline specs under openspec/specs/.

### 🔴 Critical Issues (implementation blockers)

- **native-tool-calling delta uses `## REMOVED Requirements` for two MODIFIED requirements.** `Special-case tool interception` and `Tool definition assembly` are supplied as complete, updated requirements (full scenario blocks) but sit under `## REMOVED Requirements`. OpenSpec sync/archive deletes everything under REMOVED, so this would strip both requirements entirely from openspec/specs/native-tool-calling/spec.md. Heading MUST be `## MODIFIED Requirements`. **Fixed**: delta now uses `## MODIFIED Requirements`.

- **file-access-zones delta MODIFIED requirement is truncated — 4 scenarios' behavior would be deleted.** MODIFIED replaces the entire matching requirement. Baseline `Paths are classified into zones…` has 7 scenarios; the delta supplied only 4, one truncated, and three whole scenarios missing (Path resolution uses realpath, Vault file path is UNRECOGNISED, Trust-store and vault remain UNRECOGNISED). These are security guarantees that would be silently deleted on sync/archive. **Fixed**: delta now reproduces all 7 scenarios verbatim, changing only scenario 1's directory list.

### 🟡 Should Fix

- **native `Built-in tools included`: unannounced drop of "15".** Baseline says "all 15 built-in tools"; delta said "all built-in tools." **Fixed**: restored "15" to match baseline.

- **native `create_tool is not intercepted` scenario: error wording diverges from D2.** THEN said error "indicating create_tool is not a recognized action," but frozen D2 fixes the unknown-tool string as "Tool '<name>' is not a built-in tool, MCP tool, or vision_query." **Fixed**: scenario now uses D2's exact error string.

- **Proposal "Specs Modified" is an incomplete summary (declarative omission).** Frozen proposal mentions only tools_generated/; the delta correctly follows design D8 (removes both tools/ and tools_generated/; removes Script tools excluded). Delta is correct. Declarative omission in frozen proposal, addable via soft-freeze note.

### ✅ What's Done Well

- file-access-zones scenario 1 directory list edit exactly matches D8 line 118 and preserves the "use dedicated built-in tools" clause.
- native delta correctly scoped to only the two affected requirements; other baseline requirements untouched.
- Substantive native edits are correct: plan/vision_query scenarios preserved verbatim, PSEUDO_TOOL_SCHEMAS narrowed to plan only, Script tools excluded removed with the right rationale.

### ⚖️ Review Verdict

Not ready to advance until both 🔴 format issues are fixed. Both fixed in this round.

## specs Round 2 — 2026-07-23

Re-review of the specs batch (native-tool-calling, file-access-zones) against Round 1 findings. proposal.md and design.md are FROZEN (baselines). Verified against both frozen artifacts and the two baseline specs under openspec/specs/.

### 🔴 Critical Issues (implementation blockers)

None. Both Round 1 🔴 format issues are resolved.

- **native heading REMOVED→MODIFIED — RESOLVED.** Delta line 3 is `## MODIFIED Requirements`; both requirements supplied as complete updated requirements. Sync/archive will now update (not delete) them.
- **file-access-zones truncation — RESOLVED.** Delta reproduces all 7 scenarios, changing only scenario 1's directory list. No security guarantee silently dropped.

### 🟡 Should Fix

None outstanding.

- **native "15" — RESOLVED.** Restored "all 15 built-in tools" (matches baseline).
- **native create_tool error wording — RESOLVED.** Uses D2's exact string.
- **proposal "Specs Modified" declarative omission — still open, accepted.** Design and delta agree; soft-freeze note, no unfreeze required.

### ⚖️ Review Verdict

**Ready to freeze.** All Round 1 🔴 (2) and 🟡 (4) items resolved and verified against source.

## tasks Round 1 — 2026-07-23

Reviewed tasks.md against frozen proposal.md, design.md, and both delta specs. All task line references verified against live source.

### 🔴 Critical

- **`create_tool` confirmation subsystem only half-removed — contradicts design D4.** Task 4.2 removed only `request_tool_create()`. Verified still present: confirmation.py `signal_tool_create` (L185), `get_pending_tool_create` (L181), state dicts (L55-57), class docstring §3 (L40-41); agent_controller.py `get_pending_tool_create` (L237) + `resume_tool_create` (L241); telegram_callbacks.py `cb_tool_create` (L136) + `tool_create_*` branches; telegram_interface.py import (L51), handler reg `^tool_create_` (L266), `_send_tool_create_prompt` (L568/L742), buttons (L763-765). D4 says ConfirmationManager "no longer handles tool-creation approval flows" — as tasked it still does. Frozen conflict: proposal "Kept" declares telegram_callbacks.py unaffected. **Triggered unfreeze of proposal.md.** Fixed: task 4 expanded to 5 subtasks covering all 6 files; proposal Kept list corrected.

- **Prompt-template task 9.3 names wrong file → risks leaving create_tool live in system prompt.** Actual surface: `prompts/system/05-response-format.md:27` (create_tool action def), `prompts/system/04-execution.md:30` (tool-creation rule), `prompts/system/03-capabilities.md:14`, `prompts/sub-agent/04-response-format.md:14`. **Fixed**: task 9 rewritten with exact file names and line references.

### 🟡 Should Fix

- react_loop.py:863 "Unknown action" message still says `Use "tool", "create_tool", or "finish"`. **Fixed**: task 3.6 added.
- Task 14.3 insufficient for test_agent_runtime_characterization.py: assertions L140-141 will AttributeError. **Fixed**: task 14.4 added.
- config.toml.example:244 `generated_tools_dir` not covered. **Fixed**: task 13.3 added.
- README task 13.2 under-specified: tools refs at L125, L154, L230, L505, L522-523. **Fixed**: task 13.2 expanded.
- agent_controller.py module docstring L10 not covered. **Fixed**: task 6.4 added.
- Extra test files: test_file_tools_zone.py:97, test_access_control.py:23. **Fixed**: tasks 14.10-14.11 added.
- Stale refs: schemas.py:5 docstring, patterns.py:26-27 comment. **Fixed**: tasks 4.1 and 10.1 expanded.

### ⚖️ Review Verdict

NOT READY — triggered proposal unfreeze. All findings addressed in the unfreeze cascade.

## proposal Round 3 — 2026-07-23

Re-review after UNFREEZE. Trigger: tasks Round 1 found telegram_callbacks.py and telegram_interface.py affected by create_tool removal but listed as "Kept."

### 🔴 Critical Issues

None. Every create_tool/tool_create code symbol maps to a scope item across all 6 files.

### 🟡 Should Fix

- **Kept-list contradiction: `builtin_tools/*` "entire" vs. two files in Modified.** Kept line claimed "entire built-in tool system" but Modified edits `schemas.py` and `patterns.py`. **Fixed**: Kept line qualified to note schemas.py and patterns.py receive doc/pattern edits.

### ⚖️ Review Verdict

**Ready to re-freeze.** Unfreeze is clean — all create_tool surfaces in scope, no contradictions, Kept list accurate.

## design Round 3 — 2026-07-23

Re-review after proposal UNFREEZE (proposal Round 3 clean). Focus: D4 expansion to full create_tool confirmation subsystem. proposal.md is RE-FROZEN (baseline).

### 🔴 Critical Issues

- **`telegram_callbacks.py` still listed as "untouched" in "What is NOT changing."** Contradicted D4 item 5 and the re-frozen proposal's Modified section. **Fixed**: removed from "What is NOT changing."

### 🟡 Should Fix

- **`builtin_tools/*` "entirely untouched" stale.** Contradicted D4 item 2 (schemas.py) and D6 (patterns.py). **Fixed**: qualified to note schemas.py/patterns.py receive doc/pattern edits.
- **D4 header count mismatch.** Said "six places" but listed seven. **Fixed**: changed to "seven places."

### ⚖️ Review Verdict

**Ready to re-freeze.** All issues fixed — design consistent with re-frozen proposal.

## tasks Round 2 — 2026-07-23

Re-review of tasks.md against re-frozen proposal.md and updated design.md. All task line-references verified against live source.

### 🔴 Critical Issues

- **Task 9.2 too narrow — would leave live create_tool guidance in system prompt.** `prompts/system/04-execution.md` has a `TOOL CREATION AND EXECUTION RULES:` block (lines 29-43). Task 9.2 cited only "line 30, etc." but the tool-creation content spans lines 29-30 and 34-40 (header, tool-creation rules, operator confirmation). `make check` cannot catch this (behavioral prompt content). **Fixed**: task 9.2 rewritten to enumerate exact lines for removal (29-30, 34-40), rename header to `EXECUTION RULES:`, and explicitly note lines 31-33 and 41-43 are retained.

### 🟡 Should Fix

- **agent_controller.py imports 41-42 not covered.** `from tool_creator import ToolCreator` and `from tool_executor import ToolExecutor` import from modules being deleted. **Fixed**: task 6.4 added.
- **telegram_interface.py 565-571 orphan branch missing.** Task 4.5 removed `_send_tool_create_prompt` definition but not its caller/`__TOOL_CREATE__:` dispatch branch. **Fixed**: task 4.5 expanded to include lines 565-571.
- **test_access_control.py L37/L169 not covered.** Beyond the L23 kwarg, the file reads `paths.generated_tools_dir` (L37) and `paths.tools_dir`/`paths.generated_tools_dir` (L169). **Fixed**: task 14.10 expanded with L37 and L169.

### ⚖️ Review Verdict

**Ready to freeze.** All Round 1 🔴 (2) and 🟡 (7) items resolved. Tasks are complete, ordered logically, and each is actionable and ≤2 hours.

## specs Final Review — 2026-07-23

Final pre-apply review of both delta specs against the two FROZEN baselines (proposal.md, design.md), the explore brief, and the two live baseline specs under openspec/specs/. Confirms the Round 2 fixes hold and checks for any residual references to the removed surface.

### 🔴 Critical Issues

None. Both Round 1 blockers remain resolved:
- native-tool-calling delta uses `## MODIFIED Requirements`; both requirements supplied as complete updated requirements.
- file-access-zones delta reproduces all 7 baseline scenarios verbatim; only scenario 1's directory list changed.

### 🟡 Should Fix

- **native `Special-case tool interception` requirement text contained change-log narration.** The sentence "The `create_tool` interception is removed — the `create_tool` action no longer exists." was retrospective delta-narration that reads awkwardly as permanent baseline. **Fixed**: rewrote to steady-state phrasing: "The ReAct loop SHALL intercept `plan` and `vision_query` native tool calls before `_dispatch_tool()` and route them to their existing special-case handlers. `create_tool` is not a recognized tool and SHALL NOT be intercepted; it is handled as an unknown tool by `_dispatch_tool()`."

### ✅ What's Done Well

- native `Tool definition assembly` reproduces full baseline, narrows PSEUDO_TOOL_SCHEMAS to `plan` only, preserves "all 15 built-in tools", correctly drops "Script tools excluded" per D8's structural-property rationale.
- native `create_tool is not intercepted` scenario uses D2's exact error string and is consistent with the design's cross-module data-flow diagram.
- file-access-zones scenario 1 drops both `tools/` and `tools_generated/`, leaving `data/, skills/, prompts/, log dir, vault dir`. Three security-critical scenarios (realpath bypass, vault UNRECOGNISED, trust-store/vault carve-out) preserved verbatim.
- All three requirement names match baselines exactly for correct sync targeting.
- Completeness confirmed by grep: no other spec under openspec/specs/ references create_tool, tools_generated, tools_dir, ToolExecutor, ToolCreator, or hand-written/script tools — no additional delta specs required.

### ⚖️ Review Verdict

**Ready to advance to apply.** No blockers. Deltas are format-correct, content-correct against frozen proposal + design D2/D8, complete (no orphaned references), and unambiguous for an implementer.
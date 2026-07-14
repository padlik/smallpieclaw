## proposal Round 1 — 2026-07-14

**Batch:** proposal (first batch, no frozen artifacts). Baseline: `explore-brief.md`.

### 🔴 Outstanding
1. Modified-capability mismatch: `agent-runtime-construction` does not own loop execution behavior. The only legitimate touchpoint is caching `ctx._tool_defs` on `ReactContext` — a field addition, not a spec-level behavior change.
2. `vision_query` native-dispatch contradiction: `builtin-tool-execution` spec requires `vision_query` to be executed by the ReAct loop, not executor dispatch. A native `vision_query` call would hit `_dispatch_tool()` and error out.
3. MCP tool schemas dropped from "What Changes" — brief commits to MCP tools but proposal only mentions built-in tools.
4. Error-handling trichotomy partially represented — proposal is silent on `LLMPermanentError` propagation.

### 🟡 Addressed
*(none — first round)*

### ✅ Fixed
*(none — first round)*

---

## proposal Round 2 — 2026-07-14

**Batch:** proposal. Verdict: **passes — all four Round 1 issues resolved.**

### 🔴 Outstanding
*(none)*

### 🟡 Addressed
*(none)*

### ✅ Fixed
1. Modified-capability mismatch: `agent-runtime-construction` removed. New capability `native-tool-calling` now owns the loop-side integration.
2. `vision_query` native-dispatch: now intercepted before `_dispatch_tool()` alongside `create_tool`/`plan`.
3. MCP tool schemas: added to "What Changes" scope.
4. `LLMPermanentError` propagation: explicitly stated in fallback description.

---

## design Round 1 — 2026-07-14

**Batch:** design (second batch). Frozen: `proposal.md`. Baselines: `explore-brief.md`, ADR-0007, ADR-0008.

### 🔴 Outstanding
1. Text-fallback mechanism contradictory: design.md:126 said "fall through to chat_with_fallback(json_mode=True)" (re-query), but design.md:231 and explore-brief.md:37 said parse-in-place. Every `finish` triggers this branch — re-query costs a redundant LLM call per task completion.

### 🟡 Addressed
2. `tool_schemas.py` placement diverges from ADR-0008 — should be in `builtin_tools/` package.
3. Source of `create_tool`/`plan` pseudo-tool schemas unspecified.
4. Streaming / `progress_cb` behavior under native tool calls undefined.
5. Component diagram misrepresents data flow — request edges labeled with response type, provider methods feed intercept directly.

### ✅ Fixed
*(proposal Round-1 resolutions verified as correctly carried into design)*
1. Capability ownership — scoped under `native-tool-calling`, `_tool_defs` on `ReactContext`.
2. `vision_query` interception — routed to `_exec_vision_query()` before `_dispatch_tool()`.
3. MCP tool schemas — present in scope and flow.
4. `LLMPermanentError` propagation — explicit in Decision 4 error handling.

---

## design Round 2 — 2026-07-14

**Batch:** design. Verdict: **passes — all five Round 1 issues resolved.**

### 🔴 Outstanding
*(none)*

### 🟡 Addressed
- Soft-freeze: `proposal.md` path references updated from `tool_schemas.py` to `builtin_tools/schemas.py` (declarative path-label drift from fixing Round 1 issue #2).

### ✅ Fixed
1. Text-fallback mechanism: now parse-in-place (no re-query). Three-way response handling: tool_calls → native dispatch, text → parse_json in place, error → fresh json_mode call.
2. `tool_schemas.py` → `builtin_tools/schemas.py` (co-located with `descriptors.py` per ADR-0008).
3. `create_tool`/`plan` schema source: `PSEUDO_TOOL_SCHEMAS` dict with rationale.
4. Streaming: native requests are non-streaming, `progress_cb` for retry/fallback notifications only.
5. Component diagram: request edges labeled "messages + tools", responses return to `native` node, parse-in-place path present.

---

## specs+adr Round 1 — 2026-07-14

**Batch:** specs + adr (third batch). Frozen: `proposal.md`, `design.md`. Baseline: `explore-brief.md`.

### 🔴 Outstanding
1. Spec "dispatch each tool call" (spec.md:12-13, plural) contradicts frozen single-tool-per-turn decision (design.md:253, adr/0009:38, explore-brief.md:22). Decision-level conflict; fix is spec-side (conform to frozen design) — no unfreeze needed.

### 🟡 Addressed
2. Missing error branch: design.md:140 / explore-brief.md:133 commit to "other exceptions → log warning, fall through to json_mode"; spec has no scenario.
3. Native result feedback (spec.md:13) omits the paired assistant `tool_calls` message that design.md:147-163 requires before the `role:"tool"` message.

### ✅ Fixed
*(carry-forward from frozen artifacts verified)*
1. Parse-in-place / no re-query (design Round-1 fix) correctly captured — spec.md:19.
2. vision_query interception → _exec_vision_query() present — spec.md:57-60.
3. All four design Decisions map to ≥1 requirement; 15 built-ins, pseudo-tools, script-exclusion consistent with explore-brief.
4. ADR-0009 MADR-minimal, seq 0009 verified (0001-0008 exist), supersession graph correct; manifest references 0007/0008 in-force + new 0009.

---

## specs+adr Round 2 — 2026-07-14

**Batch:** specs + adr. Verdict: **passes — all three Round 1 issues resolved.**

### 🔴 Outstanding
*(none)*

### 🟡 Addressed
*(none)*

### ✅ Fixed
1. "dispatch each tool call" → "dispatch the first tool call" with single-tool-per-turn note.
2. Catch-all error scenario added: unexpected exception → log warning → fall through to json_mode.
3. Native result feedback now describes assistant+tool message pair (assistant with tool_calls block, followed by role:"tool" message).

---

## tasks Round 1 — 2026-07-14

**Batch:** tasks (fourth and final batch). Frozen: proposal.md, design.md, specs, adr.

### 🔴 Outstanding
1. Missing test for "Tool definitions cached" scenario (spec.md:94-97).
2. Google and Ollama provider payload scenarios have no test task (spec.md:139-147).
3. No explicit task for `NotImplementedError`-raising branch for unsupported providers (e.g., `anthropic`).

### 🟡 Addressed
*(none — first round)*

### ✅ Fixed
*(carry-forward from frozen artifacts verified as correctly reflected in tasks)*
1. Single-tool-per-turn → task 4.4 dispatches, no multi-tool task.
2. Parse-in-place / no re-query → task 4.4 "text → parse_json in place".
3. vision_query interception → task 4.5 + test 7.6.
4. Native result feedback (assistant + tool pair) → task 4.6.
5. Full error trichotomy incl. LLMPermanentError + catch-all → task 4.7 + test 7.7.

---

## tasks Round 2 — 2026-07-14

**Batch:** tasks. Verdict: **passes — all three Round 1 issues resolved.**

### 🔴 Outstanding
*(none)*

### 🟡 Addressed
*(none)*

### ✅ Fixed
1. "Tool definitions cached" test → task 7.1a: assert single invocation + reuse across multi-step run.
2. Google & Ollama payload tests → tasks 7.3a/7.3b: assert `tools`/`tool_choice` in payload, `response_format` excluded.
3. `NotImplementedError` branch → task 3.1: providers without native implementation SHALL raise `NotImplementedError`.

**Full spec-to-task traceability confirmed: all 21 spec scenarios have both implementation and test tasks.**

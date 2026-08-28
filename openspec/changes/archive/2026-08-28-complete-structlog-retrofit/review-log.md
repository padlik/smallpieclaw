# Review Log — complete-structlog-retrofit

## proposal Round 1 — 2026-08-27

Reviewer: @openspec-reviewer (background task ope-1 / ses_fbac96390ffe3K0RnLUm3HuqKU)
Baseline: explore-brief.md (frozen: none — first batch)

### 🔴 Fixed
(none)

### 🟡 Addressed
- Impact line-count double-counted (implied 9 removals; actual 7 across react_loop 5 + llm_client 2 + builtin_executor 1 log_event) → restated precisely in Impact.
- ERROR-event drop folded under "non-breaking" → now disclosed as a behavior change with pre-removal verification requirement in Behavior notes.
- Unconditional/static routing property missing from proposal → clause added ("static and enablement-independent").
- `_tool_span` "MCP path" label imprecise (precedent lives in the react_loop MCP tool-span lifecycle helper at ~1796, not `_tool_span` itself) → wording corrected; apply step re-confirms the carrier function.
- merge_contextvars overwrite hazard not carried forward → proposal now points to design.md for the rationale; design must flag it so the fire-and-forget decision isn't re-litigated.

### 🔴 Outstanding
(none)

**Verdict: PASS** → proposal.md frozen (after declarative 🟡 cleanups applied).
## design Round 2 — 2026-08-27

Reviewer: @openspec-reviewer (reused session ope-1)
Baseline: frozen proposal.md + explore-brief.md

### 🔴 Fixed
(none — verdict PASS)

### 🟡 Addressed
- Inverted merge_contextvars precedence claim (design D3, inherited from brief) → verified against pinned structlog 26.1.0 (`setdefault` — explicit fields win, no overwrite hazard); rationale restated on correct footing: identity absent by default per ADR-0004; binding pointless for run-less daemon. Brief corrected with strikethrough + note.
- ERROR becomes emitted-by-nobody enum member → D2 now states it explicitly as a reserved value (descriptor/schema filter values remain valid).
- "MCP tool-span" label imprecise → D5 relabeled "tool-event lifecycle helper"; precedent located in `_emit_tool_lifecycle` (~1795), MCP qualifier dropped; apply re-confirms carrier.
- XDG property name diverged from convention → `graph_memory_log_file` renamed `graph_memory_log` (mirrors `graph_memory_db`).
- D2 lacked a named rejected alternative → added ("keep ERROR + TOOL_FAILED" rejected as double-counting per react_loop precedent).

### 🔴 Outstanding
(none)

**Verdict: PASS** → design.md frozen (after declarative 🟡 cleanups applied; brief's Option-A reason 2 corrected declaratively — no decision changed).

## specs Round 3 — 2026-08-27

Reviewer: @openspec-reviewer (reused session ope-1)
Baseline: frozen proposal.md + design.md + explore-brief.md

### 🔴 Fixed
- Delta contradicted untouched "Structured JSONL event sink" requirement ("any component → both sinks" becomes false for isolated graph_memory) → added MODIFIED entry for that requirement: Rule scoped with component-isolation exception, "any component" scenario narrowed to "any component not subject to component log isolation"; both original scenarios preserved (JSONL-parseable unchanged).

### 🟡 Addressed
- ERROR reserved/zero-emitter status not pinned → Rule now states ERROR is reserved, no emitters after this change, valid zero-match filter value.
- Two Rule: blocks around one Feature: in ADDED requirement → merged into a single Feature+Rule pair (stylistic).

### 🔴 Outstanding
(none)

**Verdict: PASS (after fix loop)** → specs frozen.

## specs Round 4 — 2026-08-27

Reviewer: @openspec-reviewer (reused session ope-1) — re-review after Round-3 fix loop
Baseline: frozen proposal.md + design.md + explore-brief.md

### 🔴 Fixed (verified from Round 3)
- MODIFIED "Structured JSONL event sink" verified correct: header exact, full block copied, Rule carve-out + narrowed WHEN, both original scenarios preserved, contradiction with ADDED requirement eliminated.
- ERROR reserved-member line verified.
- Dual Rule merge verified content-complete.

### 🟡 Addressed
(none)

### 🔴 Outstanding
(none)

**Verdict: PASS** → specs frozen (2 MODIFIED + 1 ADDED requirements).

## adr Round 5 — 2026-08-27

Reviewer: @openspec-reviewer (reused session ope-1)
Baseline: frozen proposal.md + design.md + specs + explore-brief.md

### 🔴 Fixed
(none — verdict PASS)

### 🟡 Addressed
- Manifest listed ADR-0019 twice (own bullet + interactions list + range) → trimmed; union still covers 0001–0022 exactly once.

### 🔴 Outstanding
(none)

**Verdict: PASS** → adr.md + adr/0023-record-exactly-once-lifecycle-logging.md frozen. ADR-0004 untouched (IRON RULE holds); ADR-0023 is a companion, Supersedes: None.

## tasks Round 6 — 2026-08-27

Reviewer: @openspec-reviewer (reused session ope-1)
Baseline: frozen proposal.md + design.md + specs + adr.md + adr/0023 + explore-brief.md

### 🔴 Fixed
(none — verdict PASS)

### 🟡 Addressed
- Three spec scenarios lacked explicit regression guards → 6.1 now asserts LLM_FAILED emitted exactly once; 6.3 now asserts component records carry no trace/agent identity; backfill CLI remains a manual verify task (4.4, integration-flavored per R3/R4).
- 7.1 vulture invocation omitted `--exclude interfaces.py` → aligned with AGENTS.md `make lint` command.

### 🔴 Outstanding
(none)

**Verdict: PASS** → tasks.md frozen. All five planning batches (proposal → design → specs → adr → tasks) passed review; planning complete.

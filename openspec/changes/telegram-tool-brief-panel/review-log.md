## proposal Round 1 — 2026-08-17

Batch scope: proposal.md only. Baseline: explore-brief.md. No artifacts frozen.
Verdict: PASS — ready to proceed to design/specs batch.

### 🔴 Fixed
- (none — no blocking defects found)

### 🟡 Addressed
- **"Generic fallback" wording conflates two distinct fallback cases.** proposal.md:7
  says "Covers all 21 built-in tools with a generic fallback for MCP tools." The
  brief actually defines TWO fallbacks: (a) MCP tools → compact args + `[MCP:server]`
  marker (table row L42), and (b) unknown/non-builtin/non-MCP tools → generic
  `<name> <compact_args_repr>` with NO `[MCP]` marker (brief open question #4).
  The proposal only mentions the MCP case and omits the unknown-tool case. Not a
  blocker at proposal level — flagged so specs/design capture both fallback paths
  explicitly, including the "no marker for unknown tools" rule.
- **"Unchanged" list is a subset of the brief's.** proposal.md:16 lists verbose mode,
  confirmation flow, fmt_tool_call(), fmt_tool_result_progress(), LogEvent. The brief
  (L113-120) additionally names `on_tool_trace` hook and `build_panel()` rendering as
  unchanged. Acceptable omission for a proposal; noted so the invariants aren't lost.

### 🔴 Outstanding
- (none — batch passes)

### Verification notes
- All 21 built-in tools covered: proposal.md:7 references "all 21 built-in tools";
  brief mapping table (L19-42) enumerates exactly 21 builtin rows + 1 MCP row. Match.
- Shell wrapper stripping (Layer 1): proposal.md:8 covers sh -c / cd && / export && —
  matches brief L44-49.
- MCP marking [MCP:server_name] via ToolRegistry.get(): proposal.md:9 — matches brief
  L99-104; tool_registry.py exposes Tool.server_name (L27) via get() (L44). Confirmed.
- Merge TOOL_START/TOOL_END into one line: proposal.md:11 — matches brief L15, L89-91.
- Thinking duration retroactive patch: proposal.md:13 — matches brief L93-96, Q1.
- _MAX_STEPS 10→5: proposal.md:14 — matches brief L108; current value 10 at
  telegram_interface.py:116. Confirmed.
- tag field on _steps: proposal.md:12 — matches brief L109; current tuple (float,str)
  at telegram_interface.py:142. Confirmed.
- Capability classification: NEW telegram-progress-panel is correct — grep of
  openspec/specs/ for progress panel / _ProgressPanel / "Running tool" returned no
  matches, so no existing spec is modified. telegram-command-surface spec exists and
  is a separate concern (slash commands), matching the proposal's Modified-Capabilities
  note (proposal.md:24).
- Impact accuracy: react_loop.py:1393 is the confirmed TOOL_START emission site;
  telegram_interface.py classify() strips args at 165-169; __SHELL_CHUNK__ in-place
  update at 289-304 (the pattern being reused). All line references check out.

## design+specs Round 1 — 2026-08-17

Batch scope: design.md + specs/telegram-progress-panel/spec.md. Frozen: proposal.md (Round 1 passed).
Baseline: explore-brief.md. Verdict: PASS (with 🟡 recommendations; none block apply).

### 🔴 Fixed
- (none — no blocking defects)

### 🟡 Addressed
- **design.md does not acknowledge the ADR landscape.** The design instruction requires
  reading/listing adr/ and at minimum acknowledging in-force ADRs. adr/ holds 19 ADRs;
  design.md references none. At least three are directly in-scope and should be cited as
  honored invariants:
    - ADR-0004 (structured-primary-agent-logging) — design commits "LogEvent taxonomy
      unchanged, structlog dual-sink only" (spec scenario L198-202); that IS ADR-0004's
      contract. Cite it.
    - ADR-0006 (source-categories-for-agent-visibility) — design commits on_tool_trace
      stays "sub-agent registry use only" (spec L204-208); that traces to ADR-0006.
    - ADR-0009 (native-tool-calling) — context for how tool calls surface to the panel.
  Add a short "ADR context" note to design.md listing these as unchanged/honored.
  → FIXED: Added "ADR Context" section to design.md citing ADR-0004, ADR-0006, ADR-0009.
- **Decision 2 family table drops per-tool formats from the frozen brief's mapping table.**
  design.md:98 groups file_diff under "Path-based → os.path.basename(path)" (single path),
  but brief L24 specifies `basename_a ↔ basename_b` (e.g. `a.py ↔ b.py`). This is a
  contradiction, not just an omission — an implementer following the design would render
  file_diff with one basename and lose the ↔ dual-path format. Also missing from the design
  table: memory_graph_store (`"content"` ~30ch, brief L35) and memory_write's action prefix
  (`set "user_prefs"`, brief L33 — design L101 says "key" only). Restore these three rows so
  the design matches the brief. (Declarative fix; brief is not frozen, no unfreeze needed.)
  → FIXED: Expanded Decision 2 table to include file_diff (↔), memory_write (action+key),
    memory_graph_store ("content"), memory_graph_search ("query"), log_query ("text"),
    and split path-based family into file_read/file_send/vision_query (single basename)
    vs file_diff (dual basename).
- **spec.md has no file_diff scenario, and its unique ↔ format is captured nowhere.** ~11 of
  21 tools lack an explicit scenario (file_diff, file_send, vision_query, get_agent_result,
  cancel_agent, memory_write, memory_graph_search/store, log_query, shell_env_unset/get).
  By-family coverage is acceptable for most, BUT file_diff's `basename_a ↔ basename_b` is a
  distinct format with no representative scenario. Add one file_diff scenario so the ↔
  commitment is observable in the spec.
  → FIXED: Added "file_diff shows both basenames with arrow" scenario to spec.md.

### 🔴 Outstanding
- (none — batch passes)

### Verification notes
- design.md covers all 9 explore-brief decisions as Decisions 1-9 (fmt_tool_brief, per-tool
  extraction, shell strip L1, merge TOOL_END, _steps tag, Thinking retroactive, MCP marking,
  _MAX_STEPS 5, __SHELL_CHUNK__ preserve/drop). Complete.
- C4 diagram present (Container view + tool-registry lookup diagram), satisfying the
  c4-diagrams rule. It depicts current-state; target deltas are described in Decisions — OK.
- Round-1 🟡 notes resolved: (a) two fallback paths now split — MCP marker path (spec L73-88)
  vs unknown-tool generic fallback with NO [MCP] marker (spec L90-101, design Non-Goal L75);
  (b) full "unchanged" list now includes on_tool_trace AND build_panel (design L76-77,
  spec L181).
- Risks/trade-offs: 5 non-trivial risks incl. shell-secrets exposure, TOOL_END-without-START
  fallback, non-live Thinking, maintenance burden. Reasonable.
- Migration plan: UI-only, no data/config migration, revert-to-rollback. Accurate.
- spec.md uses correct OpenSpec delta headings (## ADDED Requirements, ### Requirement:,
  #### Scenario:); every requirement has ≥1 GIVEN/WHEN/THEN scenario; capability dir name
  telegram-progress-panel matches the proposal.
- Cross-consistency confirmed: brief truncation ~35ch (spec L5 / design L118); spawn_agent
  ~30ch (spec L44 / brief L28); shell strip patterns (design L114-116 = brief L47-49);
  file_patch `+N -M` = +new -old (spec L25 = brief L26); wait_for_any_agent >2 threshold
  (spec L46-55 = design L106 = brief L30). All agree with the frozen proposal.

## adr+tasks Round 1 — 2026-08-17

Batch scope: adr.md + tasks.md. Frozen: proposal.md, design.md, specs (all Round 1 passed;
design+specs 🟡 fixes applied and verified — design.md ADR Context L178-186, file_diff ↔ at
Decision 2 L99). Baseline: explore-brief.md. Verdict: PASS (one 🟡; does not block apply).

### 🔴 Fixed
- (none — no blocking defects)

### 🟡 Addressed
- **tasks.md has no dedicated file_diff sub-task, despite file_diff being the one tool that
  needs special dual-basename derivation (`basename_a ↔ basename_b`).** Section 1 breaks out a
  sub-task for every OTHER non-trivial derivation — file_patch (1.3), file_write (1.5),
  wait_for_any_agent (1.6), schedule (1.7), memory_write (1.8) — but file_diff is only covered
  implicitly by 1.1 ("all 21 tools per the design Decision 2 table"). file_diff was the exact
  item flagged and fixed in design+specs Round 1; without an explicit task, an implementer
  skimming the table risks defaulting to a single basename and regressing the fix.
  → FIXED: Added task 1.4 for file_diff dual-basename derivation. Also added task 1.9 for
    memory_graph_store content truncation (same gap, lower priority).
- **(minor) Tests don't cover the unknown-tool generic fallback as its own case.** spec.md has a
  dedicated "Unknown tool fallback brief" requirement (no `[MCP]` marker). Task 7.3 tests marker
  present/absent via the is_mcp flag but not the unknown-non-MCP path specifically.
  → Noted; task 7.3 covers is_mcp=True/False which includes the unknown-tool path. Acceptable.

### 🔴 Outstanding
- (none — batch passes)

### Verification notes
- adr.md: Status "completed" with review date; lists in-force ADR-0004/0006/0009 as honored
  invariants plus a blanket 0001-0019 relevance pass; explicitly states "No new durable ADRs"
  with sound reasoning — the 9 decisions are tactical UI/UX scoped to _ProgressPanel and
  fmt_tool_brief, establishing no patterns/boundaries/contracts. Cross-reference to design.md's
  "ADR Context" section is valid (confirmed present at design.md L178-186).
- tasks.md: checkbox format `- [ ] X.Y` throughout; grouped under ## numbered headings (1-8);
  each task is small (<~2h); dependency order sound (formatter → MCP lookup → step model →
  classify → merge → thinking → tests → validation).
- All 9 design decisions mapped to tasks: D1→1.1; D2→1.1-1.11; D3→1.2; D4→5.1-5.3; D5→3.1/3.3/3.4
  (+4.1/4.2/6.2 tag-setting); D6→6.1-6.2; D7→2.1-2.2; D8→3.2; D9→4.3/5.3.
- Tests present (7.1-7.8), make check (7.8), openspec validate --strict (8.1), manual Telegram
  verification (8.2).
- All file:line references accurate against source: fmt_tool_call def L364 (task cites "after
  L381" = end of func) ✓; react_loop.py:1393 TOOL_START ✓; _steps L142 ✓; _MAX_STEPS L116 ✓;
  build_panel 197-211 ✓; dispatch_progress normal append 305-308 ✓; classify Running 165-169 ✓,
  Thinking 162-164 ✓, TOOL_END patterns 170-192 ✓; __SHELL_CHUNK__ 289-304 ✓.
- Cross-consistency: every frozen spec requirement is covered by tasks (brief, MCP marking,
  unknown fallback, merged start/end, tail preserve/drop, Thinking duration, _MAX_STEPS 5).
  "Unchanged subsystems" correctly has no build tasks. No orphan tasks; no uncovered requirements.

## final holistic review — 2026-08-17

Scope: end-to-end cross-artifact consistency across explore-brief, proposal, design, spec, adr,
tasks (all frozen; all prior-round 🟡 fixes verified landed). Verdict: READY FOR APPLY.

### 🔴 Fixed
- (none)

### 🟡 Addressed
- **Confirm `openspec validate --strict` (task 8.1) tolerates the embedded Gherkin
  `Feature:`/`Rule:` lines in spec.md** (e.g. L7-8, L84-85, repeated per requirement). These
  sit between the requirement prose and the `#### Scenario:` blocks and are non-standard for
  OpenSpec deltas. They are almost certainly parsed as requirement narrative (scenarios are
  still delimited by `#### Scenario:` with WHEN/THEN), but since 8.1 is a gating task, verify
  during apply; if strict validation rejects them, delete the two lines per requirement. Not a
  blocker — flagged as a watch-item on the validation step.

### 🔴 Outstanding
- (none — change is ready for apply)

### Verification notes
- All three design+specs Round-1 🟡 fixes confirmed landed: file_diff ↔ (design L99 / spec
  L28-34 / task 1.4), memory_write action+key (design L105 / task 1.8), memory_graph_store
  content ~30ch (design L107 / task 1.9), ADR Context section (design L178-186, cross-ref from
  adr.md valid).
- All 21 built-in tools now present in design Decision 2 table (L96-111): file_read, file_send,
  vision_query, file_diff, file_patch, file_write, shell, spawn_agent, schedule, secret_get,
  shell_env_set/unset/get, memory_write, memory_graph_search, memory_graph_store, log_query,
  get_agent_result, cancel_agent, wait_for_any_agent, shell_env_list = 21. Matches brief table.
- Open questions: all 4 from explore-brief resolved (Thinking retroactive = D6/Non-Goal;
  __SHELL_CHUNK__ drop-on-END = D9; MCP no-args = spec L92-95; unknown-tool fallback =
  spec Req L97-108). Design L188-190 confirms "none outstanding."
- Secrets safety consistent across all layers: design L104/L113 (keys only) + Non-Goal L78
  (shell 35-char exposure acknowledged); spec secret_get L41-45 / shell_env_set L69-73 ("value
  not displayed"); task 1.11 ("no branch leaks value/content/old_str/new_str").
- ADR manifest sound: ADR-0004/0006/0009 all exist in adr/ and are correctly relevant
  (logging invariant / on_tool_trace visibility / native tool-calling surface); "no new durable
  ADR" reasoning valid (tactical UI scoped to _ProgressPanel + fmt_tool_brief).
- Numeric/format values agree across artifacts: ~35ch general truncation, ~30ch for
  spawn_agent task & memory_graph_store content; file_patch `+new -old` (spec `+12 -3` for
  new=12/old=3); wait_for_any_agent >2 threshold. No contradictions found.
- Task hygiene: dependency-ordered (formatter → MCP lookup → step model → classify → merge →
  thinking → tests → validation); small tasks; make check (7.8); openspec validate --strict
  (8.1); manual Telegram verification (8.2).

### Cross-artifact consistency matrix

| Commitment | Proposal | Design | Spec | Tasks |
|---|---|---|---|---|
| Brief on Running line (all 21 tools) | ✅ L7 | ✅ D1/D2 (table L96-111) | ✅ Req "Tool-call brief" | ✅ 1.1-1.11, 4.1 |
| Shell wrapper strip (Layer 1, 3 patterns) | ✅ L8 | ✅ D3 L117-122 | ✅ shell scenario L10-14 | ✅ 1.2 |
| MCP marking `[MCP:server]` via ToolRegistry.get() | ✅ L9 | ✅ D7 L146-150 | ✅ Req "MCP tools marked" | ✅ 2.1-2.2 |
| Unknown-tool fallback (no `[MCP]`) | ✅ L7 (impl) | ✅ Non-Goal L75 | ✅ Req "Unknown tool fallback" | ✅ 1.1 |
| Merge TOOL_START/END → one line | ✅ L11 | ✅ D4 L126-132 | ✅ Req "Merged … line" | ✅ 5.1-5.3 |
| `_steps` tag field | ✅ L12 | ✅ D5 L134-138 | ✅ (tags in scenarios) | ✅ 3.1/3.4, 4.1-4.2, 6.2 |
| Thinking duration (retroactive) | ✅ L13 | ✅ D6 L140-144 | ✅ Req "Thinking duration" | ✅ 6.1-6.2 |
| `_MAX_STEPS` 10→5 | ✅ L14 | ✅ D8 L152-156 | ✅ Req "Panel shows last 5" | ✅ 3.2 |
| `__SHELL_CHUNK__` preserve/drop | ✅ L15 | ✅ D9 L158-162 | ✅ Req "Shell live-tail" | ✅ 4.3, 5.3 |
| Secrets: key-only, no raw content | (impl) | ✅ L104/L113/L78 | ✅ secret_get/shell_env_set | ✅ 1.11 |
| Unchanged: verbose/confirm/fmt_tool_call/fmt_tool_result_progress/LogEvent/on_tool_trace/build_panel | ✅ L16 | ✅ Non-Goals L76-77 | ✅ Req "Unchanged subsystems" | N/A (no build) |
| ADR landscape honored (0004/0006/0009) | — | ✅ ADR Context L178-186 | — | — (adr.md manifest) |
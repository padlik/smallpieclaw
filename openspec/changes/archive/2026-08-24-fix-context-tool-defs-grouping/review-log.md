## Full Review — 2026-08-24

### Proposal
- **[pass]** Accurately describes all three bugs and their fixes. Capabilities are correct: New = none; Modified = `context-monitoring`, `mcp-oauth-flow` — matches the two files under `specs/`. Impact section names the three touched files plus tests.

### Design
- **[pass]** All three decisions are sound and well-justified. Decision 1 (`builtin_names` set vs. registering builtins in `ToolRegistry`) correctly preserves the MCP-only registry contract. Decision 2 (register in the Telegram handler, not `_run_oauth_flow`) correctly avoids a new MCP→registry dependency and mirrors `/mcp on`. Decision 3 (partial `dataclass_replace` refresh) is consistent with the existing idle-transition pattern. Risks/trade-offs and alternatives are identified. Component diagram is accurate.

### Specs (context-monitoring)
- **[pass]** The MODIFIED requirement "Tool definitions sub-categorised by MCP server" reproduces the full base content and adds the builtin-classification paragraph, extended Rule, and new scenario. The ADDED requirement "Context snapshot refreshable outside the ReAct loop" has five well-formed scenarios (4 hashtags, testable). Fixed: moved from MODIFIED to ADDED section.

### Specs (mcp-oauth-flow)
- **[pass]** The MODIFIED "OAuth authorization flow for MCP servers" requirement reproduces the full base content — all five intro paragraphs preserved, plus the new registration paragraph. All 16 existing scenarios preserved. The two scenarios that gained an `AND` (Successful OAuth flow → register tools; Re-authentication → re-register tools) and the new "Tools registered in ToolRegistry after OAuth success" scenario are all properly formatted.

### ADR
- **[pass]** Manifest is correct: status `completed`, correctly concludes no new durable ADRs are needed. ADR-0022 listed as the in-force ADR reviewed.

### Tasks
- **[pass]** All tasks use `- [ ]` checkboxes, ordered by dependency, small enough for one session. Full spec coverage. Task 3.1 now explicitly includes `BUILTIN_TOOL_SCHEMAS`/`PSEUDO_TOOL_SCHEMAS` imports and `builtin_names` construction. Task 4 includes ruff, vulture, `make check`, and `openspec validate --strict`.

### 🔴 Outstanding
- _(none — all issues fixed)_

### 🟡 Addressed
- `builtin_names` construction now explicitly specified in Task 3.1 with imports and set-building logic.
- Proposal preserved-fields list now matches the spec (system prompt tokens, chat history tokens, completion reserve, effective window, compaction threshold, and turn number).
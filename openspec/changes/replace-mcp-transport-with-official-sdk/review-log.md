## proposal Round 1 — 2026-07-15

### 🔴 Outstanding
- **`requests` removal contradicts baseline.** proposal.md:9 and :27 commit to removing `requests` from requirements.txt, but explore-brief.md:100 (Open Q#3) resolved to KEEP it pending transitive verification, and the brief's Files Changed table (:88) never lists its removal. Baseline artifacts disagree. Grep confirms `import requests` is only in mcp_client.py, which supports removal — so reconcile by updating brief Open Q#3 to "verified, safe to remove," and make both docs agree.

### 🟡 Suggestions
- **Capability name leaks implementation.** `mcp-sdk-transport` (:17) names a behavioral capability after the SDK. Rename to behavior-oriented (`mcp-transport` / `mcp-tool-communication`); let design.md own the SDK choice. Decide before specs, since the spec path derives from this name.
- **"Zero behaviour change" vs subprocess-death handling.** Proposal claims zero caller/spec behaviour change (:3, :21), but Open Q#1 (:96) changes dead-stdio handling: old code auto-restarts; new path relies on SDK reconnection else returns an error dict. Acknowledge the connection-loss contract so the capability spec includes a server-down/connection-loss scenario (otherwise it'll be missed → rework).
- **Implementation detail leakage.** "mock mcp.client.Client" (:26) is a test strategy and "background event loop thread" (:8) is architecture — both belong in design.md/tasks.md, not the proposal.
- **Verify SDK symbols in design.** Official MCP Python SDK high-level client is typically `ClientSession` (+ `mcp.client.stdio.stdio_client`), not `mcp.client.Client`. Confirm real symbol names in design.md before tasks.
- **Line-count inconsistency.** Why says "~500 lines" (:3); Impact says "~750 → ~250" (:25). State consistently.

### 🟢 OK
- "Not changed" list (:28) matches the brief's Files NOT Changed (:92) exactly — clean scope boundary.
- All three changed files covered (mcp_client.py, tests/test_mcp_client.py, requirements.txt); no orphan impacts.
- New-vs-Modified classification correct: verified no existing MCP spec under openspec/specs/, so a new capability is right; empty Modified section is explained.
- "Why" is clear and compelling (LOC reduction, protocol negotiation, typed results, zero caller changes).

## proposal Round 2 — 2026-07-15

### 🔴 Fixed
- **requests removal contradiction**: explore-brief.md Open Q#3 updated to "verified, safe to remove." Both docs now agree.
- **Capability name**: changed from `mcp-sdk-transport` to `mcp-transport` (behavior-oriented, not implementation-leaking).
- **Connection-loss behaviour**: added explicit mention in What Changes section acknowledging the change from auto-restart to error-dict return.
- **Implementation detail leakage**: removed "mock mcp.client.Client" and "background event loop thread" from proposal.
- **Line-count inconsistency**: unified to "~750 → ~250" throughout.

## proposal Round 2 — 2026-07-15 (Reviewer Verdict)

### 🔴 Outstanding
- None. All five Round 1 issues resolved. Proposal is internally consistent and baseline-consistent.

### 🟡 Suggestions
- Capability blurb (proposal:18) still names the "official mcp Python SDK" — name is now behavioral but the description leaks the HOW. Trim to behavior when writing the spec; let design.md own the SDK.
- Carry-forward to design (Round 1 #4, not yet actioned): brief still uses `mcp.client.Client`/`Client(url)` (brief:21-22,75,86-87). Real SDK API is `ClientSession` + `stdio_client`/`streamablehttp_client`. Confirm in design.md before tasks — not a proposal blocker.
- Brief Files Changed table (brief:88) requirements.txt row is stale: omits the `requests` removal and the `<2.0` upper pin now agreed in proposal:28 / Open Q#4. Tidy for baseline self-consistency.

### 🟢 OK
- requests removal now agrees across proposal (:9,:28) and brief Open Q#3 (:100).
- Capability renamed to behavior-oriented `mcp-transport`.
- Connection-loss contract explicit (proposal:12) and scoped into the capability (:18) — prevents a missed server-down scenario → no apply-time rework.
- Implementation-detail leakage removed from proposal (:8,:27 now generic).
- Line counts reconcile: ~500 transport lines removed; ~750→~250 file total (750−500=250).
- "API contract unchanged" vs connection-loss behavior change reconcile cleanly (return-shape preserved, only auto-restart behavior differs).

### ⚖️ Verdict
Ready to freeze the proposal and advance to the design batch. Mandatory design gate: verify real SDK symbol names before tasks.

## design Round 1 — 2026-07-15

### 🔴 Outstanding
- **Mandatory design gate NOT closed — symbols deferred, and the committed ones are wrong.** Round 2 verdict made "verify real SDK symbol names" a hard precondition. Design used `Client` (should be `ClientSession`), `streamable_http_client` (v2 name; v1.x is `streamablehttp_client` returning 3-tuple). Open Questions section re-opened the gate instead of closing it.
- **Async boundary design incomplete — anyio same-task constraint unaddressed.** D2 decomposed the wrapper into three independently-dispatched coroutines, but `stdio_client` + `ClientSession` are anyio context managers whose cancel scopes must be entered and exited on the same task. Entering in one `run_coroutine_threadsafe` call and exiting in another triggers RuntimeError. The realistic pattern (single long-lived session-runner coroutine + request queue) was not described.

### 🟡 Suggestions
- **stdio `env` regression**: D3 passed `env=cfg.env` alone, but current code merges `os.environ` first. SDK does not inherit parent env — stdio servers would launch without PATH.
- **Server error-state maintenance dropped**: Brief committed that protocol-mismatch RuntimeError is "Logged, server marked 'error' in list_servers()". Design collapsed everything to "return an error dict" without per-server connected/error state.
- **Outcome dict `exit_code` field**: Current `_tool_outcome` includes `exit_code`. Design D4 omitted it.
- **`timeout` mapping omitted**: Brief mapped timeout to SDK, but design never stated where per-server `cfg.timeout` is wired.
- **camelCase/snake_case hazard**: D4/D5 used `result.is_error`/`t.input_schema` but v1.x SDK uses `isError`/`inputSchema`.
- **C4 diagram mislabeled**: Titled "Container" but everything is in one process — should be "Component". Edge labels carried wrong symbol names.

### 🟢 OK
- Scope consistent with frozen proposal — no scope creep.
- D1 alternatives + rationale solid.
- Migration plan concrete and rollback-safe.
- Open questions from brief resolved.
- No task-level leakage.

### ⚖️ Verdict
Not ready. Two blockers: close the symbol gate against v1.x, and redesign the async bridge around the anyio same-task constraint.

## design Round 2 — 2026-07-15

### 🔴 Outstanding
- None. All 8 Round 1 issues are resolved (symbol gate closed against v1.x, async bridge redesigned around the anyio same-task constraint, env merge, server error-state, exit_code, timeout wiring, camelCase attrs, C4 relabel). Design is implementable and consistent with the frozen proposal.

### 🟡 Suggestions
- **`asyncio.Queue` is not thread-safe — D7 claim was factually wrong.** `asyncio.Queue` is not thread-safe; `put()` must run on the loop thread. Fixed: D7 now states cross-thread dispatch goes through `run_coroutine_threadsafe(queue.put(req), loop)`.
- **Connect-ready handshake described two ways.** D1 used `self._ready.set()` (asyncio.Event) while D9 used `future.result()`. Fixed: unified to `concurrent.futures.Future` (`_ready_future`) for cross-thread-safe await.
- **`list_tools()` pagination parity gap.** Current code follows pagination cursors; D1 called `list_tools()` once. Fixed: session runner now loops on `nextCursor` to collect all tools, preserving parity.

### 🟢 OK
- Symbol gate genuinely closed: `stdio_client` → `(read, write)`, `streamablehttp_client` → 3-tuple with third discarded, `StdioServerParameters(command/args/env)` all match v1.x.
- D4 content-type table is complete and current for v1.x with correct `.type` discriminators.
- HTTP headers resolved cleanly: `streamablehttp_client(cfg.url, headers=cfg.headers)` — one code path, no regression.
- Scope matches frozen proposal exactly; Non-Goals mirror the Impact "not changed" list.
- Session-runner catches all SDK exceptions inside the loop (D6) so no new exception types leak past mcp_client.py.
- D8 connection-loss behaviour is consistent with the frozen proposal:12 (error dict, no auto-restart).
- Migration plan and rollback remain concrete and data-migration-free.

### ⚖️ Verdict
Ready to advance to the specs/tasks batch. No blockers. All 🟡 items fixed.

## specs Round 1 — 2026-07-16

### 🔴 Outstanding
- None. All scenarios trace to real mcp_client.py behavior or frozen design commitments; `#### Scenario:` headers all use four hashes; connection-loss contract (proposal:12) is captured.

### 🟡 Suggestions
- **Dropped `type == "error"` content item.** mcp_client.py:104-105 currently maps `type == "error"` → `[error] {text}`; this is absent from frozen D4 and the spec's content-type scenarios. Not a standard MCP content type (errors use `isError`), so the drop is likely intentional — but confirm explicitly to avoid a silent regression at apply time. Declarative omission in frozen D4: addable via soft-freeze, no unfreeze chain needed.
- **stdio parent-env inheritance** (D3 regression risk) has no dedicated scenario — only implicitly covered by "Successful connection on startup."
- **Protocol-version mismatch at connect** (brief/D6 distinct "error" case) is subsumed under "Connection failure on startup"; a one-line note would close traceability.
- **Timeout scenario** could explicitly state the server stays `active` (per D9), since timeout ≠ connection loss.

### 🟢 OK
- exit_code 0/1 matches `_tool_outcome` (mcp_client.py:69) — correct MCP contract, not the builtin `-1`.
- Error strings match code verbatim (mcp_client.py:663,666) — no caller/test drift.
- Connection-loss requirement encodes no-auto-restart (proposal:12 / D8) across stdio/HTTP/timeout; timeout correctly omits status flip while death scenarios assert status → error.
- Content-type scenarios (incl. SDK-new audio/resource_link) track frozen D4 exactly; mixed content joined by newlines matches mcp_client.py:115.
- Tool name conflict (first-wins + warning) and empty-name skip match mcp_client.py:638-644 / 76-78.
- Full public-API coverage (connect_all/close_all/get_tools/has_tool/call_tool/list_servers/set_enabled).
- All-ADDED structure correct (no prior MCP spec); empty MODIFIED/REMOVED explained.

### ⚖️ Verdict
Ready to advance to the tasks batch. No blockers.

## tasks Round 1 — 2026-07-16

### 🔴 Outstanding
- None. No task misdirects implementation; all five frozen migration-plan steps are represented.

### 🟡 Suggestions
- **stdio env-merge has no task or test.** design D3 (:124) requires merging `os.environ` with `cfg.env` (SDK does not inherit parent env; stdio servers lose PATH otherwise). Flagged in design R1 🟡 and specs R1 🟡, still unclosed. Task 2.2 only says "enter transport context" — add an explicit env-merge subtask under 2.2 and a test under 6.2 asserting os.environ keys are passed to StdioServerParameters. Highest-priority item.
- **Paginated discovery untested.** Spec "paginated server" (spec:46-49) + design R2 nextCursor loop (D1) implemented in 2.2, but 6.2 has no multi-page list_tools test — single-page happy path would pass with the cursor loop unexercised.
- **Tool-name-conflict untested.** Spec conflict scenario (spec:51-54, first-wins + warning) implemented in 3.2 but absent from 6.3.
- **get_server_info scenarios untested.** Spec "Detailed server info" / "Unknown server info" (spec:138-146) kept in 3.5, but 6.3 only names list_servers and §7 never exercises `/mcp info`. Add found + not-found coverage.
- **Dropped `type == "error"` content mapping not tracked.** specs R1 carry-forward: current code maps error-type content; D4 omits it. No task confirms the removal — add a one-line confirmation task to avoid a silent regression.
- **Helper/wrapper ordering inverted.** 2.2 calls `_sdk_tools_to_registry`/`_sdk_result_to_outcome` (section 4); section 3 depends on 2+4. Reorder helpers (4) before/with the wrapper (2), or note stubbing.
- **`requests` removal (1.2) precedes the rewrite that still imports it (5.3).** Frozen migration plan removes requests at step 4 (after rewrite). Harmless (editing requirements.txt doesn't uninstall) but reorder to match the plan.

### 🟢 OK
- All tasks use `- [ ] X.Y` checkbox format (:3-50).
- Five migration-plan steps fully covered: add dep (1.1), rewrite internals (§2-5), rewrite tests (§6), remove requests (1.2), integration (§7).
- Smoke test fully mapped: /mcp list (7.1) → tool via ReAct (7.2) → /mcp off (7.3) → /mcp on (7.4) → make check (7.5).
- D4 content types (text/image/resource/audio/resource_link/mixed/isError) all tested in 6.4; D5 mapping in 6.5.
- Timeout wiring (D9) split into connect (2.3) and call (2.4) with a timeout test (6.2).
- Old-transport removal thorough: 5.1 classes, 5.2 methods, 5.3 imports, 5.4 constants.

### ⚖️ Verdict
Ready to advance to apply with the 🟡 items recommended (chiefly the stdio env-merge task/test — a twice-flagged regression trap with no current guard). No blockers.

## tasks Round 1 Fixes — 2026-07-16

All 🟡 items addressed:
- **stdio env-merge**: task 3.2 now explicitly says "merge `os.environ` with `cfg.env`"; test 6.2 includes "assert `os.environ` keys passed to `StdioServerParameters`"
- **Paginated discovery**: test 6.2 now includes "paginated `list_tools` (multi-page with `nextCursor`)"
- **Tool-name-conflict**: test 6.3 now includes "tool name conflict (first-wins + warning)"
- **get_server_info**: test 6.3 now includes "`get_server_info` (found + not found)"; integration 7.5 added `/mcp info` test
- **Dropped `type == "error"`**: task 2.1 now includes explicit note that the old mapping is intentionally dropped
- **Helper/wrapper ordering**: helpers moved to section 2, wrapper to section 3, MCPManager to section 4
- **`requests` removal**: moved to 5.5 (after rewrite, matching migration plan step 4)

## tasks Round 2 — 2026-07-16

### 🔴 Outstanding
- None. All 7 tasks Round 1 🟡 items resolved: stdio env-merge (3.2 + test 6.2 asserting os.environ→StdioServerParameters), paginated list_tools (6.2), tool-name-conflict (6.3), get_server_info found+not-found (6.3 + integration 7.5), dropped `type=="error"` note (2.1), helper/wrapper/manager ordering (§2/§3/§4), requests removal moved to 5.5 (after rewrite, matching migration step 4). Format, dependency order, design→task, spec-scenario→task, migration-plan steps, and smoke-test all verified complete.

### 🟡 Suggestions
- close_all has no manager-level test: 6.2 covers wrapper close(), but 6.3 omits close_all, so the "Graceful shutdown" scenario (spec:32-35 → task 4.3) is un-unit-tested. Add to 6.3.
- "Disabled server skipped on startup" (spec:17-20) implemented in 4.2 but not asserted in any test — add one assertion in 6.3 that an enabled:false server stays unconnected/off.
- D7 threading.Lock (protects _tool_to_server / _wrappers against agent-thread reads vs main/Telegram-thread writes) has no explicit task callout in §4; likely subsumed in 4.2-4.4 but a one-line mention would prevent it being dropped at apply time.

### 🟢 OK
- All 7 Round 1 🟡 items verified fixed against frozen design.md/spec.md.
- All 24 spec scenarios trace to at least one task + one test.
- All 9 design decisions (D1-D9) have corresponding implementation tasks.
- Migration-plan 5 steps and the smoke-test sequence (7.1→7.4 + 7.5/7.6) fully mapped.
- Checkbox format `- [ ] X.Y` throughout; dependency ordering deps→helpers→wrapper→manager→removal→tests→integration is correct.

### ⚖️ Verdict
Ready to advance to apply. No blockers. Three optional 🟡 test-coverage tightenings noted.

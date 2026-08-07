## proposal Round 1 — 2026-08-08

### 🟡 Fixed
 - `_PROBE_POST_HEADERS` removal contradiction → kept `_PROBE_POST_HEADERS` (reused for both new POSTs), removed only `_PROBE_TOOL_NAME`

### 🟢 Addressed
 - Added acknowledgment that current probe already does GET→POST(dummy) sequence
 - Noted that spec scenario needs correction (not just update) since it documents behavior that never worked

### 🔴 Outstanding
 - None

**Verdict: Batch passes.** proposal.md is frozen.

## design Round 1 — 2026-08-08

### 🔴 Outstanding
 - SSE parsing: design committed to `response.json()` but `tools/list` response may be SSE-framed (Accept header includes `text/event-stream`)
 - D3 fallback condition missing `probe_saw_auth_challenge` guard — would fall back even when OAuth fired on tools/list 401
 - Design didn't confirm whether `tools/list` requires preceding `initialize`/session

## design Round 2 — 2026-08-08

### 🔴 Fixed
 - SSE parsing: added "Response parsing" paragraph to D1 — handles both `application/json` and `text/event-stream` framings. Confirmed via curl that Gmail returns `application/json`.
 - D3 fallback guard: added explicit `probe_saw_auth_challenge` guard — if tools/list returns 401, event hook fires and fallback is suppressed
 - Initialize prerequisite: D1 now explicitly states tools/list does not require preceding initialize/session (curl-confirmed)

### 🟢 Outstanding
 - Risk block bullets still say "JSON parsing" — cosmetic, D1's Response parsing paragraph is authoritative

**Verdict: Batch passes.** design.md is frozen.

## specs Round 1 — 2026-08-08

### 🟡 Outstanding
 - INFO vs WARNING on tools/call 200: spec says INFO/no-warning, design says WARNING for arg-error-before-auth edge case
 - "200 on both" scenario missing tools/list step — references "real tool name" without preceding tools/list
 - No SSE scenario — design commits to SSE parsing but no scenario exercises it

## specs Round 2 — 2026-08-08

### 🟡 Fixed
 - INFO vs WARNING: "200 on all" scenario now explicitly covers GET + tools/list (non-empty) + tools/call all returning 200 → INFO, no warning. Design's WARNING is for the distinct arg-error-before-auth edge case.
 - Missing tools/list step: scenario renamed to "Proactive probe returns 200 on GET, tools/list, and tools/call" — GIVEN now includes tools/list returning 200 with non-empty list
 - SSE scenario: added "tools/list returns SSE-framed response — probe parses data frames and extracts tool name"

### 🟢 Outstanding
 - SSE scenario terminal outcome could link to 401/200 scenarios (optional)
 - Requirement prose line 11 attaches headers only to tools/call (cosmetic, scenario line 53 covers both)

**Verdict: Batch passes.** specs are frozen.

## adr+tasks Round 1 — 2026-08-08

### 🟡 Fixed (soft-freeze additions to tasks.md)
 - Added test 2.6: GET 405 → tools/list → tools/call 401 (missing GET-405 branch coverage)
 - Added test 2.8: tools/list returns 500 (non-200, non-401) → WARNING, no tools/call (D3 non-200 branch)
 - Removed `_PROBE_LIST_HEADERS` ambiguity from task 1.2 (keep `_PROBE_POST_HEADERS` for both POSTs)

### 🟢 Outstanding
 - Task 1.3 could explicitly state setting final_status from tools/list response (covered by 1.7)
 - adr.md lists ADRs as bare range (acceptable for bug fix)
 - Task 4.3 runs only OAuth test files (optional make check step)

**Verdict: Batch passes.** adr.md and tasks.md are frozen. All artifacts complete.
## proposal Round 1 — 2026-07-24

### 🔴 Fixed
(first round — no prior issues)

### 🟡 Fixed
- `get()` / `by_trace()` scope boundary not stated — added "Unchanged methods" bullet to What Changes
- Search text-only exclusion not stated — added "(trace_id search is out of scope for this change)" to Search bullet

### 🔴 Outstanding
(none)

### 🟡 Outstanding
(none)

## design Round 2 — 2026-07-24

### 🔴 Fixed
(none from Round 1 — all Round 1 issues were 🟡)

### 🟡 Addressed
All 7 Round 1 issues verified correct:
- Lock scope for `search()` → D4 explicit 4-step sequence + "Lock scope:" callout ✅
- `_evict_oldest()` guard condition → D2 states "only when len > MAX_IN_MEMORY" ✅
- Empty query after time-window extraction → D7 states search("", days=7) = wildcard ✅
- `cmd_prompts` rendering → D8 added: show-found, show-not-found, search results, empty-results ✅
- Risk 3 rewritten → clean sequential statement ✅
- Risk 4 rewritten → references lock-free scan from D4 ✅
- `_archive_snapshot()` named in D1 ✅

### 🔴 Outstanding
(none)

### 🟡 Outstanding
- Case-insensitive qualifier absent from D4 → added "case-insensitive" to step 2
- `/prompts show` missing-argument not handled → added guard in D7 for len(ctx.args) < 2

## specs Round 1 — 2026-07-24

### 🔴 Fixed
(first round — no prior issues)

### 🟡 Addressed
- Missing scenario: `/prompts search` with no query and no time window → added "Search with empty query and no time window returns most recent prompts" scenario
- Missing scenario: `/prompts show` on a running record → added "Show on a running prompt displays elapsed time without end timestamp" scenario

### 🔴 Outstanding
(none)

### 🟡 Outstanding
(none)

## adr Round 1 — 2026-07-24

### 🔴 Fixed
(first round — no prior issues)

### 🟡 Addressed
All 6 review checks verified:
- Durable decision (not tactical) ✅
- Consistent with frozen design.md (D1–D6) ✅
- Manifest lists all 13 in-force ADRs ✅
- Manifest references new ADR-0014 file ✅
- Sequence number 0014 correct (one greater than 0013) ✅
- No contradictions with frozen artifacts ✅

### 🔴 Outstanding
(none)

### 🟡 Outstanding
(none)

## tasks Round 1 — 2026-07-24

### 🔴 Fixed
(none)

### 🟡 Addressed
- Task 1.5 test condition wording inverted ("runs once when archive exists" → "skips when archive already exists")
- Added archive-file-absent test cases to tasks 3.3 and 4.3
- Added hours-to-fractional-days conversion note to task 5.2

### 🔴 Outstanding
(none — reviewer flagged missing `list_recent()` sort task, but `list_recent()` already sorts by `started_at` at `prompt_registry.py:250`; no regression introduced by backfill)

### 🟡 Outstanding
(none)
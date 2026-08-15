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

## proposal Round 2 (amendment) — 2026-08-15

### 🔴 Fixed
(none)

### 🟡 Addressed
- Missing `days` vs `since`/`until` precedence rule in Search bullet → added "when both `days` and `since`/`until` are supplied, `since`/`until` take precedence and `days` is ignored" to proposal.md line 9
- Help-text bullet omitted new filter flags → updated `telegram-command-surface` capability bullet to mention `--status`, `--trace`, `--since`, `--until`, `--page` in help text

### 🔴 Outstanding
(none)

### 🟡 Outstanding
(none — reviewer noted to carry forward Open Questions #2 (unknown-flag → query text) and #3 (--page 1-indexed) into design.md; both are already in explore-brief amendment and will be captured in the design batch)

## design Round 3 (amendment) — 2026-08-15

### 🔴 Fixed
(none — read-only review; findings listed under Outstanding)

### 🟡 Addressed
(none)

### 🔴 Outstanding
- Decision 4 ⨯ Decision 8 contradiction: `search()` returns only page slice (`list[PromptRecord]`) but Decision 8 pagination footer needs `total_matched` for `Page N of total_pages` rendering — data-flow gap makes footer unimplementable

### 🟡 Outstanding
- `since`/`until` timezone handling under-specified: naive ISO inputs interpreted in host-local timezone, producing silently wrong filtering for non-UTC deployments
- CLI `limit` implicitly fixed at 20 but not stated — `offset = (page-1) * limit` and `total_pages = ceil(total_matched / limit)` depend on this unstated assumption

## design Round 4 (amendment) — 2026-08-15

### 🔴 Fixed
- Decision 4 ⨯ Decision 8 contradiction → `search()` return type changed to `SearchPage` dataclass (`results: list[PromptRecord]` + `total_matched: int`); Decision 4 step 4 records `total_matched` before slicing; "Return contract" paragraph added; Decision 8 footer consumes `total_matched` from `SearchPage`; explore-brief signature updated to `-> SearchPage`

### 🟡 Addressed
- Timezone handling → "Timezone handling" paragraph added to Decision 4: naive ISO inputs (no offset) interpreted as UTC via `dt.tzinfo is None` check + `timezone.utc` replacement before `.timestamp()`; explicit offsets honored as-is
- Fixed CLI limit → "Pagination" paragraph now states "The CLI does not expose `--limit`; page size is fixed at the `search()` default of 20"
- Out-of-range page rendering → Decision 8 pagination footer now includes: when `results` is empty but `total_matched > 0`, render "Page N is past the last page" instead of no-matches message
- Explore-brief stale "TBD" on unknown-flag handling → updated to "treated as query text"
- Explore-brief API table `search()` row → added "(superseded — see amendment)" marker
- Proposal impact section → added `SearchPage` dataclass to `prompt_registry.py` impact bullet (soft-freeze declarative addition)

### 🔴 Outstanding
(none)

### 🟡 Outstanding
(none — reviewer noted to carry into specs batch: add scenarios for out-of-range-page rendering and naive-timezone since/until boundary)

## specs Round 2 (amendment) — 2026-08-15

### 🔴 Fixed
(none — read-only review; findings listed under Outstanding)

### 🟡 Addressed
(none)

### 🔴 Outstanding
- Last-page pagination footer contradicts frozen design Decision 8: spec scenario "Search pagination with --page returns the next page" asserts page 2 of 2 shows footer `Page 2 of 2`, but frozen design only shows footer "when total_matched exceeds the returned page" — on the last page no footer is shown. Design Decision 8 must be unfrozen and updated to a two-part footer (always show "Page N of M", conditionally append "next" hint).

### 🟡 Outstanding
- Out-of-range page message contradicts frozen design's empty-result rendering: spec correctly says page 5 of 2 → "Page 5 is past the last page" but frozen design Decision 8 still says empty results → "No prompts matching" with no out-of-range carve-out (deferred from design Round 2)
- "Search with combined filters" scenario has two WHEN/THEN pairs — malformed Gherkin, should be split into two single-trigger scenarios

## design Round 5 (amendment) — 2026-08-15

### 🔴 Fixed
- Decision 8 unfrozen and updated: pagination footer is now a two-part line — always `📄 Page <N> of <total_pages>` when `total_matched > 0`, plus ` — use --page=<N+1> for next` only when `offset + len(results) < total_matched`; on last page shows footer without tail; single-page shows `📄 Page 1 of 1`; out-of-range page (empty results + total_matched > 0) shows "Page N is past the last page" instead of no-matches message

### 🟡 Addressed
- Header `<count>` clarified as `total_matched` (not page slice size) so header agrees with `total_pages` in footer

### 🔴 Outstanding
(none)

### 🟡 Outstanding
(none)

## specs Round 3 (amendment) — 2026-08-15

### 🔴 Fixed
- Last-page footer contradiction → resolved by design Decision 8 unfreeze (Round 5): two-part footer now makes page 2 of 2 show `Page 2 of 2` (no tail), matching spec scenario

### 🟡 Addressed
- Out-of-range page message → design Decision 8 now includes carve-out; spec scenario matches
- Combined-filters scenario split → two properly-formed single-WHEN scenarios: "Search with combined status and trace_id filters" (no match) and "Search with status filter alone narrows results" (only failed prompt)
- Single-page footer scenario added: "Search with results fitting on a single page shows a single-page footer" — 5 matches → `📄 Page 1 of 1` (no tail)

### 🔴 Outstanding
(none)

### 🟡 Outstanding
(none — reviewer noted to carry into tasks batch: ensure tasks for two-part conditional footer, out-of-range branch, and Page 1 of 1 single-page case)

## tasks Round 2 (amendment) — 2026-08-15

### 🔴 Fixed
(none)

### 🟡 Addressed
- Round 2 header-count ambiguity → task 6.1 now explicitly states `<total_matched>` from `SearchPage.total_matched` (not page slice size)
- Round 2 single-page-footer coverage gap → tasks 6.2 and 6.4 now cover `Page 1 of 1` single-page case (implementation + test)
- Eviction→archive round-trip test → task 2.2 now includes explicit round-trip assertion: finalize → evict → `show()` returns the record from archive

### 🔴 Outstanding
(none)

### 🟡 Outstanding
(none — reviewer noted optional suggestions: split large bundled test tasks 3.5/5.4/6.4 for finer granularity; task 6.3 could state "elapsed = now − started_at when running" for precision. Both are apply-time refinements, not blocking.)
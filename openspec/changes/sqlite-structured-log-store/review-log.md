# Review Log

## design Round 1 — 2026-08-31
### 🔴 Fixed
- D5 text-search regression: `msg LIKE ?` collapsed full-record substring search (msg + event_type + logger + trace + agent + prompt_id + all extra fields) into msg-only, and `COLLATE NOCASE` is ASCII-only vs current full-Unicode `.casefold()`. Contradicted frozen proposal's "all existing filters preserved". → Fixed: added `search_text` column (pre-casefolded compact JSON of full record, written once at ingestion, ~2× storage accepted); text filter now `search_text LIKE ?` with casefolded needle, semantics identical to today.
### 🟡 Addressed
- Option C event-set: design's 6-event SQL vs current frozenset {TOOL_START, TOOL_END, LLM_CALL}+WARNING+ are operationally equivalent today but not identical. → Added note: authoritative set is the 6-event SQL per brief; apply stage must not "fix" the frozenset mapping in a way that changes surfaced records.
- Trace auto-widen resolution (explicit trace > auto-widen on text/prompt_id > current-run) not documented in D5. → Added: resolution preserved unchanged, runs before SQL compilation, stays in `_exec_log_query`.
- `level IN (...)` must enumerate lowercase stored names (structlog emits lowercase). → Fixed in D5 table and schema comment.
- Row label "text / prompt_id substring" was wrong (prompt_id is exact-match). → Split into separate text and prompt_id rows.
### 🔴 Outstanding
(none after fixes — pending Round 2 confirmation)

## design Round 2 — 2026-08-31
### 🔴 Fixed
- D5 text-search regression resolved (Round 1 blocker): `search_text` column + casefolded-needle query restores full-record, full-Unicode substring semantics.
### 🟡 Addressed
- LIKE metacharacter semantics: D5 text row now specifies `instr(search_text, ?) > 0` (literal substring, 1:1 with Python `in`), with escaped `LIKE` as the equivalent alternative.
- Stale pre-fix references cleaned: Risks and Non-Goals now describe the `search_text` approach.
- D6 backfill now explicitly computes `search_text` for imported rows (avoids NOT-NULL insert failure).
### 🔴 Outstanding
(none — design.md passes and is frozen)

## specs Round 1 — 2026-08-31
### 🔴 Fixed
(none — no material issues found)
### 🟡 Addressed
- MODIFIED header "Structured JSONL event sink" retains the original requirement key intentionally — OpenSpec delta matching requires the exact original title; renaming would need REMOVED+ADDED. Conscious choice, kept as-is.
- Verified: all agent.jsonl / "primary sinks" rotation references re-pointed correctly; capability boundary (storage mechanics only in sqlite-log-store) holds; "Rotated history is not queried" contradiction fully replaced by full-retention scenarios; scenario format uniform; ADR-0025 partial supersession stated precisely; ADR-0004 file untouched per iron rule (optional back-reference in 0004 intentionally deferred).
### 🔴 Outstanding
(none — specs + adr batch passes and is frozen)

## tasks Round 1 — 2026-08-31
### 🔴 Fixed
(none — no material coverage gaps or scope violations)
### 🟡 Addressed
- 3.6 now also refreshes the descriptor's prose description text (stale bounded-tail/active-JSONL wording → full-retention SQL).
- 1.5 now explicitly routes startup connect/open failures (read-only logs dir) into the disable-with-WARNING path; 5.1 adds the startup open-failure test case.
- 5.1 adds the concurrent-emitter serialization test case.
- Confirmed: design fidelity D1–D7 traceable, instr() text-search decision carried through to 3.2, hygiene complete (checkbox format, ordering, vulture/docs/make-check/strict-validate gates), no scope creep.
### 🔴 Outstanding
(none — tasks.md passes and is frozen; change ready for apply)

## Post-freeze validator compliance — 2026-08-31
### 🟡 Addressed
- `openspec validate --strict` caught 4 scenario-name mismatches in MODIFIED blocks (archive matching requires original scenario names; renamed scenarios count as omissions). Restored original names — "Identity appears as JSON fields", "JSONL line is independently parseable", "Filter by prompt id is unambiguous across days", "Rotated history is not queried" — with the reviewed new-behavior content. Validator compliance only; no decision-level change. `openspec validate sqlite-structured-log-store --type change --strict` now passes.

## proposal Round 1 — 2026-08-31
### 🔴 Fixed
(none — no blocking issues found)

### 🟡 Addressed
- Retention + primary-sink identity were double-owned across `structured-event-logging` (modified) and `sqlite-log-store` (new). Fix: explicit boundary sentence added to proposal — storage mechanics (schema, indexes, queue ingestion, retention DELETE, crash/backup) live in `sqlite-log-store`; `structured-event-logging` only asserts the structured primary sink *is* now that store.
- "Unchanged" requirements (run identity, redaction, component isolation) have scenario text hardcoding `agent.jsonl` / "primary sinks" rotation parity — behaviorally unchanged, but specs batch must re-point those references (to the store / to the prose sink). Carried as an instruction to the specs batch.
- DB location decision was only implicit in the backup note. Fix: explicit line added (same XDG logs dir, `agent_logs.sqlite`).
- Distinct-CLI note: new `backfill_log_store.py` must not be conflated with existing `backfill_graph_memory.py` referenced in the component-isolation scenario. Carried to specs batch.

### 🔴 Outstanding
(none — batch passes, proposal frozen)
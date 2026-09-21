## 1. Store layer (D1, D2)

- [x] 1.1 `sqlite_log.py`: expose the row `id` in `row_to_record()` output (top-level `id` field) so every reader shares one record projection — inject `record["id"] = row["id"]` directly in `row_to_record`; do NOT add `"id"` to `WIDE_COLUMNS` (that tuple is shared with the write path `record_to_row`/`INSERT_EVENT_SQL`, whose column list must keep excluding `id`)
- [x] 1.2 `builtin_tools/logquery_helpers.py`: add `before_id` to `LogQueryFilters` (default `None`); extend `_log_query_compile_where` with an `id < ?` part when set — filter parts compose with all existing parts, parameters ordered to match
- [x] 1.3 Unit-check via existing helper tests (or add small cases in the test batch, 5.x): default `before_id=None` compiles to the exact same WHERE as today (no behavioral diff without the param)

## 2. `log_query` handler: `before_id` walk semantics (D2)

- [x] 2.1 `builtin_tools/secrets_log.py` `_exec_log_query`: parse `before_id` (int; JSON null / non-int / <= 0 → filter omitted, mirroring the `limit` fallback); when the filter is applied, validate that every returned record's `id` is `< before_id` (an unapplied anchor surfaces as an anomaly, never as walk progress)
- [x] 2.2 COUNT query unchanged by `before_id`: `total_matched` reports the entire match set (constant across pages on a static store; may grow on a live store — a set-size signal, never a completion signal); only the page query gets the `id < ?` part
- [x] 2.3 Page ordering: the page query is always `ORDER BY id DESC` (re-ascended for output) so consecutive `before_id` pages are gapless even when emit-time `ts` order and drain-order `id` diverge; the ordering MUST NOT be gated on which truncation path fires — the per-call output-size trim (`max_output`, applied in `_log_query_result` after the SQL sort) is unknowable at query-build time, and a count-based trigger leaves the size-trim path (count-untruncated, size-trimmed verbose records) gappable

## 3. `log_detail` handler (D3, D4, D5)

- [x] 3.1 `builtin_tools/secrets_log.py`: add `_exec_log_detail(self, args, caller_depth=0, caller_tag="")` in `LogQueryTools`: resolve `id` (invalid → EMPTY payload `{"found": false, "record": null, "dumped": false, "dump_path": null}`); open store via `connect_store` (failure → same EMPTY payload)
- [x] 3.2 Fetch by PK (`SELECT * FROM events WHERE id = ?`), reconstruct via `row_to_record`, serialize compact JSON (`ensure_ascii=False`); not-found row → EMPTY payload
- [x] 3.3 Size gate: serialized record <= 65536 chars → return full record unprojected in `{"found": true, "record": ..., "dumped": false, "dump_path": null}`
- [x] 3.4 Oversized path: write `log-detail-<id>.json` under `results_dir/<trace>/` where `<trace>` is the record's own `trace` **validated against `r-[0-9a-f]{8}`** (missing/non-conforming → current contextvars trace; stored record content is untrusted as a path component); assert the resolved dump path stays within `results_dir` before writing (0o700 makedirs, collision counter, `os.replace`); apply the vault-secret redaction pass (idempotent defense-in-depth; MUST NOT be weaker than the shell path's); on `results_dir` unset or write failure → return the preview-projected record with a note, never an error
- [x] 3.5 Oversized payload: `{"found": true, "record": <500-char-projected preview via _log_query_project>, "dumped": true, "dump_path": "<artifact path>"}`

## 4. Registry, schemas, prompt docs (D6)

- [x] 4.1 `builtin_tools/descriptors.py`: add `log_detail` entry (args: `id` required; describes full-fidelity read, 65536-char cap, dump-to-results behavior, EMPTY on not-found)
- [x] 4.2 `builtin_tools/descriptors.py`: update the `log_query` descriptor — `before_id` param, the `limit` × output-budget interplay (raising `limit` alone doesn't return more records when the size cap binds), the `limit: 1` COUNT-probe pattern, walk-then-drill (`before_id` + `log_detail`), and walk-scoping discipline: a walk ends on an empty page (never by comparing retrieved counts to `total_matched`, which grows on a live store); scope exhaustive walks by `prompt_id` or explicit `trace`; `text`-only queries auto-widen to all traces over the full retention window
- [x] 4.3 `builtin_tools/schemas.py`: add `log_detail` schema (`{id: int}`, required `["id"]`); add optional `before_id` (integer) to `log_query`
- [x] 4.4 `builtin_executor.py`: add dispatch entry routing `"log_detail"` to `self._logquery._exec_log_detail` (dispatch-symmetric signature)
- [x] 4.5 `prompts/system/03-capabilities.md` + `prompts/sub-agent/03-tools.md`: add `log_detail` rows; note `before_id` and record `id` on `log_query` rows

## 5. Tests (D7)

- [x] 5.1 `tests/test_log_query.py`: every returned record carries `id` matching the store PK
- [x] 5.2 `tests/test_log_query.py`: `before_id` paging — first page 900→851, `before_id=851` returns 850→801, strictly decreasing, no overlap; `total_matched` constant at the whole-set size across pages; the returned page is id-ordered (gapless seam into page 2)
- [x] 5.3 `tests/test_log_query.py`: walk-while-appending — inserting higher-id rows between pages shifts/duplicates/omits nothing (ids already seen never re-appear); `total_matched` grows and the walk still terminates by empty-page exhaustion, not count comparison
- [x] 5.3b `tests/test_log_query.py`: ts/id-order inversion — matching records where an older-`ts` record has a higher id than a newer-`ts` one; a full walk over the divergent set returns every record exactly once (no gap at the page-1→page-2 seam). Two paging regimes: (a) count-truncated (match set > `limit`); (b) size-trimmed — sub-`limit` count with verbose records (large `err`) forcing the `max_output` trim, asserting the trimmed page's kept ids are the highest and the walk stays gapless under inversion
- [x] 5.4 `tests/test_log_query.py`: `before_id` composes with `event_type`/`prompt_id`/`text` filters; invalid values (null, non-int, `<= 0`) omit the filter (newest page returned); all result pages are id-ordered (the always-`id DESC` rule), with the pre-existing default-view/newest-first expectations updated for id order
- [x] 5.5 New `tests/test_log_detail.py`: full-fidelity read — a >500-char `err` field returned verbatim; identity vs `log_query` preview (same `id`/`trace`/`event_type`, `err` is the untruncated source of the preview)
- [x] 5.6 `tests/test_log_detail.py`: not-found `id` → EMPTY payload; unreadable/missing store → EMPTY payload
- [x] 5.7 `tests/test_log_detail.py`: oversized record → dumped under `results/<record's trace>/`, payload carries `dumped: true` + `dump_path` + preview; no-trace record → dumped under the current run's trace; malformed stored trace (fails `r-[0-9a-f]{8}`) → dumped under the current run's trace and no directory is created from the malformed value; an oversized record containing a known vault secret is redacted in the dumped artifact (redaction preserved); dump write failure / `results_dir` unset → preview + note, `success: true`
- [x] 5.8 `tests/test_builtin_dispatch_routing.py`: routing list grows to 23 (`log_detail` routes to its handler); assert `log_detail` is not in the confirmation-capable set (exactly 8 gate: shell, file_read, file_write, file_patch, file_diff, file_send, memory_graph_store, secret_get)
- [x] 5.9 `tests/test_prompt_loader.py`: tool lists updated for `log_detail`
- [x] 5.10 Descriptor-content assertions: `log_query` descriptor text states the budget interplay, the COUNT-probe, walk-then-drill, and walk completion + scoping discipline (empty-page exhaustion, scope by `prompt_id`/`trace`, `text`-only auto-widening warning) (substring assertions over the registry text)

## 6. Static checks and validation

- [x] 6.1 `ruff check .` and `vulture . vulture_whitelist.py --min-confidence 80` pass (update `vulture_whitelist.py` only if new public API symbols are flagged)
- [x] 6.2 `make check` (lint + full test suite) passes with the known macOS-only skip as the only skip
- [x] 6.3 `openspec validate add-log-detail-tool --type change --strict` passes
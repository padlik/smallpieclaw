## 1. PromptRegistry: archive infrastructure

- [x] 1.1 Add `MAX_IN_MEMORY = 100` constant and `_archive_file_path` attribute to `PromptRegistry.__init__`
- [x] 1.2 Implement `_archive_snapshot(record: PromptRecord) -> None` — append one self-contained JSON line (7 fields: prompt_id, trace_id, text, started_at, ended_at, status, sub_agent_ids) to `data/prompts_archive.jsonl`
- [x] 1.3 Call `_archive_snapshot()` from `finish()` after updating the record and appending the event log line
- [x] 1.4 Implement `_backfill_archive() -> None` — called from `__init__` if `prompts_archive.jsonl` doesn't exist; replay `prompts.jsonl` via existing `_replay()` and write one snapshot per finalized record to the archive; skip running records
- [x] 1.5 Write tests for `_archive_snapshot()` (verify 7 fields, append-only) and `_backfill_archive()` (backfill from event log, skip running, skips when archive already exists)

## 2. PromptRegistry: in-memory eviction

- [x] 2.1 Implement `_evict_oldest() -> None` — called from `start()` only when `len(self._records) > MAX_IN_MEMORY`; find the oldest finalized record (min `started_at` among non-running), remove from `_records` and `_trace_to_id`; do nothing if all records are running
- [x] 2.2 Write tests for eviction: cap at 100, oldest finalized evicted, running records never evicted, all-running case stays at 101, evicted finalized record is still retrievable via `show()` (eviction→archive round-trip: finalize → evict → `show()` returns the record from archive)

## 3. PromptRegistry: search

- [x] 3.1 Define `SearchPage` dataclass with `results: list[PromptRecord]` and `total_matched: int` fields
- [x] 3.2 Implement `search(query="", days=None, status=None, trace_id=None, since=None, until=None, limit=20, offset=0) -> SearchPage` — acquire lock, snapshot in-memory `_records` values to local list, release lock; scan snapshot with case-insensitive substring match on `text` (empty query matches all), optional `status` exact match, optional `trace_id` exact match, optional time filter; stream `prompts_archive.jsonl` without lock, same filters, skip prompt_ids already found; merge, sort by `started_at` desc, record `total_matched = len(merged)`, skip `offset`, return `SearchPage(results=next limit, total_matched=total_matched)`
- [x] 3.3 Implement time filter logic: `since`/`until` (ISO 8601 strings parsed via `datetime.fromisoformat()`, naive inputs interpreted as UTC via `dt.tzinfo is None` → `timezone.utc` replacement before `.timestamp()`) take precedence over `days` (when both supplied, `days` ignored); `since` filters `started_at >= since_epoch`, `until` filters `started_at <= until_epoch`; `days` alone computes cutoff as `now - days*86400`
- [x] 3.4 Handle empty query as wildcard (empty string substring match returns all)
- [x] 3.5 Write tests for search: substring match, case-insensitive, relative time window (days/hours), absolute time range (since+until, since-only), naive ISO interpreted as UTC, since/until precedence over days, status filter (positive + invalid value matches nothing), trace_id exact match, combined filters (status + trace_id), empty query wildcard, dedup in-memory + archive, limit 20, offset pagination (page slice + total_matched), out-of-range offset (empty results + total_matched > 0), sorted by started_at desc, concurrent search doesn't block start(), archive file absent returns empty SearchPage

## 4. PromptRegistry: show and find_in_archive

- [x] 4.1 Implement `find_in_archive(prompt_id: str) -> PromptRecord | None` — stream `prompts_archive.jsonl` until `prompt_id` matches, return `PromptRecord` or `None`
- [x] 4.2 Implement `show(prompt_id: str) -> PromptRecord | None` — call `get()` first, if miss call `find_in_archive()`, return result or `None`
- [x] 4.3 Write tests for `find_in_archive()` (found, not found, archive file absent returns None) and `show()` (in-memory hit, archive fallback, not found in either)

## 5. Telegram commands: arg parsing and dispatch

- [x] 5.1 Extend `cmd_prompts` in `telegram_commands.py` to parse `ctx.args`: no args → list recent (unchanged); `args[0] == "search"` → search mode; `args[0] == "show"` → show mode; anything else → list recent (backward compat)
- [x] 5.2 Implement search arg parsing: split `args[1:]` into positional tokens and flag tokens (`--key=value`); recognized flags: `--status`, `--trace`, `--since`, `--until`, `--page`; extract last positional token matching `^(\d+)([dh])$` as time window (days or hours), convert hours to fractional days (divide by 24) before passing to `search()`; remaining positional tokens joined as query string; `--page=N` (1-indexed int) → `offset = (N - 1) * limit`; unknown `--` flags treated as query text (appended to positional); `--status` validated against `{running, done, failed, cancelled}` with error reply on invalid value; `--page` parsed as int with error reply on non-integer; `--since`/`--until` passed as-is to `search()` (ISO 8601 parsing at registry layer, `ValueError` caught and rendered as user-friendly error)
- [x] 5.3 Implement show arg parsing: if `len(ctx.args) < 2` reply "Usage: /prompts show <id>", else `args[1]` is the prompt_id
- [x] 5.4 Write tests for arg parsing: search with query, search with query + time window, search with only time window, search with no args, search with `--status` (valid + invalid), search with `--trace`, search with `--since`/`--until` (valid + invalid ISO), search with `--page` (valid + non-integer), search with combined flags, search with unknown flag treated as query text, show with id, show without id, unknown subcommand fallback

## 6. Telegram commands: rendering

- [x] 6.1 Implement search result rendering: header `🔍 Search results for "<query>" (<total_matched>)` where `<total_matched>` is from `SearchPage.total_matched` (not page slice size); reuse existing entry format (icon, status, timestamp, elapsed, sub-agent count, 80-char text preview); empty results with `total_matched == 0`: `ℹ️ No prompts matching "<query>".`
- [x] 6.2 Implement pagination footer rendering: always render `📄 Page <N> of <total_pages>` when `total_matched > 0` (where `N = offset // limit + 1`, `total_pages = ceil(total_matched / limit)`); conditionally append ` — use --page=<N+1> for next` only when `offset + len(results) < total_matched`; single-page results show `📄 Page 1 of 1` (no tail); out-of-range page (empty results + `total_matched > 0`): `📄 Page <N> is past the last page (<total_pages> pages total).` instead of no-matches message
- [x] 6.3 Implement show rendering: full record display (prompt_id, status icon, trace_id, start timestamp, end timestamp or "(running)" if no ended_at, elapsed time, sub-agent IDs or "none", full 200-char text untruncated); not found: `❌ Prompt <id> not found.`
- [x] 6.4 Write tests for rendering: search results with matches (page 1 of 2 with footer + tail), search results on last page (page 2 of 2 with footer, no tail), search single-page results (Page 1 of 1, no tail), search out-of-range page (Page 5 of 2, page-out-of-range message), search empty results (no-matches message), show found (done), show found (running, no ended_at), show not found, show missing arg

## 7. Help text and command registration

- [x] 7.1 Update `/help` text in `telegram_commands.py` to mention `/prompts search <query> [Nd/Nh] [--status=<S>] [--trace=<T>] [--since=<ISO>] [--until=<ISO>] [--page=<N>]` and `/prompts show <id>` subcommands, including the filter flags
- [x] 7.2 Verify `/prompts` command description in bot command registration reflects search and show capability with filter flags
- [x] 7.3 Write test for help text including search and show subcommands and all five filter flags

## 8. Lint, validate, and final checks

- [x] 8.1 Run `ruff check .` and fix any issues
- [x] 8.2 Run `vulture . vulture_whitelist.py --min-confidence 80` and update `vulture_whitelist.py` if new public API symbols are flagged (including `SearchPage` dataclass and its fields)
- [x] 8.3 Run `pytest tests/test_prompt_registry.py tests/test_prompts_command.py -v` to verify all existing and new tests pass
- [x] 8.4 Run `openspec validate add-prompt-search --type change --strict` to verify change artifacts
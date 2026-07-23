## 1. PromptRegistry: archive infrastructure

- [ ] 1.1 Add `MAX_IN_MEMORY = 100` constant and `_archive_file_path` attribute to `PromptRegistry.__init__`
- [ ] 1.2 Implement `_archive_snapshot(record: PromptRecord) -> None` — append one self-contained JSON line (7 fields: prompt_id, trace_id, text, started_at, ended_at, status, sub_agent_ids) to `data/prompts_archive.jsonl`
- [ ] 1.3 Call `_archive_snapshot()` from `finish()` after updating the record and appending the event log line
- [ ] 1.4 Implement `_backfill_archive() -> None` — called from `__init__` if `prompts_archive.jsonl` doesn't exist; replay `prompts.jsonl` via existing `_replay()` and write one snapshot per finalized record to the archive; skip running records
- [ ] 1.5 Write tests for `_archive_snapshot()` (verify 7 fields, append-only) and `_backfill_archive()` (backfill from event log, skip running, skips when archive already exists)

## 2. PromptRegistry: in-memory eviction

- [ ] 2.1 Implement `_evict_oldest() -> None` — called from `start()` only when `len(self._records) > MAX_IN_MEMORY`; find the oldest finalized record (min `started_at` among non-running), remove from `_records` and `_trace_to_id`; do nothing if all records are running
- [ ] 2.2 Write tests for eviction: cap at 100, oldest finalized evicted, running records never evicted, all-running case stays at 101

## 3. PromptRegistry: search

- [ ] 3.1 Implement `search(query: str, days: float | None = None, limit: int = 20) -> list[PromptRecord]` — acquire lock, snapshot in-memory `_records` values to local list, release lock; scan snapshot with case-insensitive substring match on `text` and optional `started_at` time filter; stream `prompts_archive.jsonl` without lock, same filter, skip prompt_ids already found; merge, sort by `started_at` desc, return top `limit`
- [ ] 3.2 Handle empty query as wildcard (empty string substring match returns all)
- [ ] 3.3 Write tests for search: substring match, case-insensitive, time window (days/hours), empty query wildcard, dedup in-memory + archive, limit 20, no matches, sorted by started_at desc, concurrent search doesn't block start(), archive file absent returns empty list

## 4. PromptRegistry: show and find_in_archive

- [ ] 4.1 Implement `find_in_archive(prompt_id: str) -> PromptRecord | None` — stream `prompts_archive.jsonl` until `prompt_id` matches, return `PromptRecord` or `None`
- [ ] 4.2 Implement `show(prompt_id: str) -> PromptRecord | None` — call `get()` first, if miss call `find_in_archive()`, return result or `None`
- [ ] 4.3 Write tests for `find_in_archive()` (found, not found, archive file absent returns None) and `show()` (in-memory hit, archive fallback, not found in either)

## 5. Telegram commands: arg parsing and dispatch

- [ ] 5.1 Extend `cmd_prompts` in `telegram_commands.py` to parse `ctx.args`: no args → list recent (unchanged); `args[0] == "search"` → search mode; `args[0] == "show"` → show mode; anything else → list recent (backward compat)
- [ ] 5.2 Implement search arg parsing: join `args[1:]` as query string, extract last token matching `^(\d+)([dh])$` as time window (days or hours), convert hours to fractional days (divide by 24) before passing to `search()`, remaining tokens as query
- [ ] 5.3 Implement show arg parsing: if `len(ctx.args) < 2` reply "Usage: /prompts show <id>", else `args[1]` is the prompt_id
- [ ] 5.4 Write tests for arg parsing: search with query, search with query + time window, search with only time window, search with no args, show with id, show without id, unknown subcommand fallback

## 6. Telegram commands: rendering

- [ ] 6.1 Implement search result rendering: header `🔍 Search results for "<query>" (<count>)`, reuse existing entry format (icon, status, timestamp, elapsed, sub-agent count, 80-char text preview); empty results: `ℹ️ No prompts matching "<query>".`
- [ ] 6.2 Implement show rendering: full record display (prompt_id, status icon, trace_id, start timestamp, end timestamp or "(running)" if no ended_at, elapsed time, sub-agent IDs or "none", full 200-char text untruncated); not found: `❌ Prompt <id> not found.`
- [ ] 6.3 Write tests for rendering: search results with matches, search empty results, show found (done), show found (running, no ended_at), show not found, show missing arg

## 7. Help text and command registration

- [ ] 7.1 Update `/help` text in `telegram_commands.py` to mention `/prompts search <query> [Nd/Nh]` and `/prompts show <id>` subcommands
- [ ] 7.2 Verify `/prompts` command description in bot command registration reflects search and show capability
- [ ] 7.3 Write test for help text including search and show subcommands

## 8. Lint, validate, and final checks

- [ ] 8.1 Run `ruff check .` and fix any issues
- [ ] 8.2 Run `vulture . vulture_whitelist.py --min-confidence 80` and update `vulture_whitelist.py` if new public API symbols are flagged
- [ ] 8.3 Run `pytest tests/test_prompt_registry.py tests/test_prompts_command.py -v` to verify all existing and new tests pass
- [ ] 8.4 Run `openspec validate add-prompt-search --type change --strict` to verify change artifacts
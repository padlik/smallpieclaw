## Why

The `/prompts` command only lists the last 20 prompts from an in-memory dict that grows unbounded — no search, no time-window filtering, no way to view a single prompt's full record. The registry loads the entire `prompts.jsonl` event log into memory on startup and never evicts, creating a slow memory leak over the agent's lifetime. Operators need to find past prompts by text content (e.g., "PTO", "worklogs") and optionally narrow by time window, without the registry holding every record in memory forever.

## What Changes

- **In-memory eviction**: `PromptRegistry` caps in-memory records at 100 (most recent finalized). Running records are never evicted. Evicted records remain accessible via the archive file.
- **Archive file**: A new `prompts_archive.jsonl` stores one self-contained snapshot line per finalized prompt (written from `finish()`). The existing `prompts.jsonl` event log is unchanged. The archive keeps full history with no rotation.
- **Search**: New `search(query, days=None, status=None, trace_id=None, since=None, until=None, limit=20, offset=0)` method scans in-memory records + streams the archive file. Case-insensitive substring match on prompt `text`, optional relative time window (days or hours) or absolute time range (`since`/`until` as ISO 8601); when both `days` and `since`/`until` are supplied, `since`/`until` take precedence and `days` is ignored. Optional status filter (`running`/`done`/`failed`/`cancelled`), optional exact `trace_id` filter, dedup by `prompt_id`, sorted by `started_at` descending, `offset` for pagination.
- **Show single prompt**: New `show(prompt_id)` method — `get()` first (in-memory), then `find_in_archive()` fallback (streams archive until `prompt_id` matches). Displays full 200-char text, trace ID, timing, and sub-agent IDs.
- **Command surface**: `/prompts` gains two subcommands:
  - `/prompts search <query> [Nd/Nh] [--status=<S>] [--trace=<T>] [--since=<ISO>] [--until=<ISO>] [--page=<N>]` — search all history with optional filters
  - `/prompts show <id>` — display a single prompt's full record
  - `/prompts` (no args) — unchanged, lists recent 20
- **Backfill**: On first startup if `prompts_archive.jsonl` doesn't exist, replay `prompts.jsonl` and write finalized records to the archive. Runs once.
- **Unchanged methods**: `get()` and `by_trace()` are not modified — they remain in-memory-only with no archive fallback. The sub-agent supervisor only needs active records.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `prompt-tracking`: Adds search, show, in-memory eviction, and archive persistence to the prompt registry. The `/prompts` command gains `search` and `show` subcommands.
- `telegram-command-surface`: The `/prompts` command description is updated to reflect the new `search` and `show` subcommands and their filter flags (`--status`, `--trace`, `--since`, `--until`, `--page`) in help text.

## Impact

- **`prompt_registry.py`**: New methods (`search`, `find_in_archive`, `show`, `_evict_oldest`, `_archive_snapshot`, `_backfill_archive`), `MAX_IN_MEMORY` constant, archive file path management. `search()` accepts `query`, `days`, `status`, `trace_id`, `since`, `until`, `limit`, `offset` and returns a `SearchPage` dataclass (`results` + `total_matched`).
- **`telegram_commands.py`**: `cmd_prompts` extended with arg parsing for `search` and `show` subcommands; flag parsing for `--status`, `--trace`, `--since`, `--until`, `--page`; new rendering for `show` output and paginated search results.
- **`data/prompts_archive.jsonl`**: New file, created on first `finish()` or backfill. Not a breaking change — the file is additive.
- **`data/prompts.jsonl`**: Unchanged format and semantics.
- **Tests**: New tests for search, show, eviction, archive write, backfill, and command arg parsing.
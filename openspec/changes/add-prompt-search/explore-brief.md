# Explore Brief: add-prompt-search

## Problem

`/prompts` lists the last 20 prompts from an in-memory dict that grows unbounded.
There is no search, no time-window filtering, and no way to view a single prompt's
full record. The registry loads the entire `prompts.jsonl` event log into memory
on startup and never evicts.

## Rejected Alternatives

1. **Replay-on-the-fly search** (scan the event log per query) — rejected because
   every search would replay the entire append-only event log (3+ lines per prompt),
   parsing 30K JSON lines for 10K prompts. Wasteful when a purpose-built snapshot
   file gives O(prompts) scanning with self-contained lines.

2. **Full rewrite to snapshot format** (replace event log, rewrite-on-every-mutation,
   like `JobExecutionLog`) — rejected because the event log is crash-safe: a process
   death between `start()` and `finish()` leaves a visible "running" record with no
   finish event. A snapshot format loses that audit trail and requires stale-record
   detection on replay.

3. **Semantic/embedding-based search** — rejected for now. Infrastructure exists
   (`tool_index.py`, `vector_utils.py`) but it's a different scope: embedding every
   prompt on ingestion, storing vectors, cosine similarity at query time. Substring
   search covers 90% of the realistic use cases at a fraction of the complexity.

## Final Approach: Dual-write + in-memory eviction

- **Keep `prompts.jsonl`** as the append-only event log (unchanged, for replay).
- **Add `prompts_archive.jsonl`** — one self-contained snapshot line per finalized
  prompt, written from `finish()`.
- **In-memory cap at 100 records** — evict oldest finalized records when exceeded.
  Running records are never evicted.
- **`search(query, days)`** scans in-memory records + streams the archive file,
  case-insensitive substring match on `text`, optional time window, dedup by
  `prompt_id`, sorted by `started_at` descending, returns top 20.
- **`show(prompt_id)`** — `get()` first (in-memory), then `find_in_archive()` fallback
  (streams archive until `prompt_id` matches). Displays full 200-char text, trace ID,
  timing, sub-agent IDs.
- **Backfill** — on first startup if `prompts_archive.jsonl` doesn't exist, replay
  `prompts.jsonl` and write finalized records to the archive. Runs once.

## Cross-Module Data Flows

```
telegram_interface._run_agent_task()
  → registry.start(trace_id, text)     [add to _records, append event, evict if >100]
  → (agent runs)
  → registry.finish(prompt_id, status) [update _records, append event, append snapshot to archive]

telegram_commands.cmd_prompts(iface, update, ctx)
  → no args:           registry.list_recent(20)
  → "search <q> [Nd]": registry.search(query, days)
  → "show <id>":       registry.show(prompt_id) → get() || find_in_archive()
```

## Command Surface

| Command | Behavior |
|---|---|
| `/prompts` | List recent 20 (unchanged) |
| `/prompts search <query> [Nd/Nh]` | Search all history (in-memory + archive) |
| `/prompts show <id>` | Full record display (in-memory, fallback to archive) |

## Arg Parsing

- `ctx.args[0] == "search"` → search mode; rest joined as query; last token matching
  `^(\d+)([dh])$` extracted as time window (days or hours).
- `ctx.args[0] == "show"` → show mode; `args[1]` is the prompt_id (full ULID).
- Anything else → list recent (backward compat).

## PromptRegistry API (new methods)

| Method | Scope | Purpose |
|---|---|---|
| `search(query, days=None, limit=20)` | Public | Scan in-memory + archive, substring + time filter |
| `find_in_archive(prompt_id)` | Public | Stream archive until prompt_id matches |
| `show(prompt_id)` | Public | get() then find_in_archive() fallback |
| `_evict_oldest()` | Internal | Called from start() when len > 100; only evicts finalized |
| `_archive_snapshot(record)` | Internal | Called from finish(); appends one line to archive |
| `_backfill_archive()` | Internal | Called from __init__ if archive file missing |

## Archive Format

One self-contained JSON object per line (not event format):

```jsonl
{"prompt_id":"01H...","trace_id":"r-xxx","text":"PTO request","started_at":1721835120.5,"ended_at":1721835312.8,"status":"done","sub_agent_ids":["sa-001"]}
```

## Constants

- `MAX_IN_MEMORY = 100` — in-memory record cap.
- Archive: no rotation, keeps forever (prompt volume is realistically small).
- Search default limit: 20 results.
- `get()` and `by_trace()` stay in-memory-only (no archive fallback) — the
  sub-agent supervisor only needs active records.

## Open Questions (resolved during explore)

1. MAX_IN_MEMORY = 100 — agreed.
2. Archive rotation — no, keep forever.
3. `/prompts show <id>` — yes, included.
4. Search by trace_id — no, handled separately if needed.
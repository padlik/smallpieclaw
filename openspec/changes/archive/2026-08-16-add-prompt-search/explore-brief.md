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
| `search(query, days=None, limit=20)` | Public | Scan in-memory + archive, substring + time filter (superseded — see amendment for expanded signature) |
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

## Amendment: Extended Search Criteria (2026-08-15)

The original design deferred four search criteria that are needed for
execution-analysis workflows (finding failed runs, recovering prompts from
trace IDs seen in `log_query` output, reviewing past incidents by absolute
date range, and paginating beyond the 20-result default). All four query
fields already present in the 7-field archive snapshot — no format change.

### Rejected Alternatives (amendment)

1. **Four separate follow-up changes** — rejected because each would need
   its own proposal/design/specs/tasks/review cycle (4× ceremony) for
   incremental one-parameter additions to `search()`. One amendment is
   far more efficient, especially since no implementation exists yet.

2. **Cursor-based pagination** — rejected for complexity. A simple `offset`
   parameter (skip N matched results) covers the "page through results"
   use case without cursor state management or archive streaming with
   bookmarks.

3. **Semantic/fuzzy search** — still rejected (same as original). The four
   additions are all exact-match or range filters on existing fields.

### Final Approach (amendment — supersedes original search design)

`search()` gains four optional parameters:

| Parameter | Type | Filter semantics |
|---|---|---|
| `status` | `str \| None` | Exact match on record `status` field. Values: `running`, `done`, `failed`, `cancelled`. |
| `trace_id` | `str \| None` | Exact match on record `trace_id` field. Reverses the original "trace_id search out of scope" decision. |
| `since` | `str \| None` | ISO 8601 timestamp; only records with `started_at >= since`. Consistent with `log_query`'s `since` semantics. |
| `until` | `str \| None` | ISO 8601 timestamp; only records with `started_at <= until`. |
| `offset` | `int` (default 0) | Skip first N matched results for pagination. |

The existing `query` (text substring) and `days` (relative time window)
parameters remain. `days` and `since`/`until` are mutually exclusive —
if both are given, `since`/`until` take precedence and `days` is ignored.

### Updated `search()` Signature

```
search(
    query: str = "",
    days: float | None = None,
    status: str | None = None,
    trace_id: str | None = None,
    since: str | None = None,      # ISO 8601
    until: str | None = None,      # ISO 8601
    limit: int = 20,
    offset: int = 0,
) -> SearchPage
```

`SearchPage` is a dataclass with `results: list[PromptRecord]` (page slice) and `total_matched: int` (full match count before offset/limit). The CLI uses `total_matched` for the pagination footer.

### Updated Command Surface

| Command | Behavior |
|---|---|
| `/prompts search <query> [Nd/Nh]` | Text search + relative time window (unchanged) |
| `/prompts search <query> --status=<S>` | Filter by status (done/failed/cancelled/running) |
| `/prompts search <query> --trace=<T>` | Filter by exact trace_id |
| `/prompts search <query> --since=<ISO> --until=<ISO>` | Absolute time range |
| `/prompts search <query> --page=<N>` | Pagination (page N, 20 per page) |
| `/prompts` (no args) | List recent 20 (unchanged) |
| `/prompts show <id>` | Full record display (unchanged) |

Filters can be combined: `/prompts search PTO --status=failed --since=2026-08-01`

### Arg Parsing (amendment)

- `ctx.args[0] == "search"` → search mode.
- Tokens are split into positional tokens (query) and flag tokens (`--key=value`).
- Recognized flags: `--status`, `--trace`, `--since`, `--until`, `--page`.
- Last positional token matching `^(\d+)([dh])$` → relative time window (days/hours), as before.
- `--page=N` → `offset = (N - 1) * limit` (1-indexed pages, default page 1).
- Unknown flags → treated as query text (avoids silent drop on typo).

### Cross-Module Data Flows (amendment)

```
telegram_commands.cmd_prompts(iface, update, ctx)
  → "search <q> [flags]": registry.search(query, days, status, trace_id, since, until, limit, offset)
  → "show <id>":         registry.show(prompt_id) → get() || find_in_archive()
```

### Open Questions (amendment — resolved)

1. `days` + `since`/`until` coexistence → `since`/`until` take precedence, `days` ignored.
2. Unknown flag handling → treat as query text (simpler, avoids silent drop).
3. `--page` indexing → 1-indexed (operator-friendly), `offset = (page - 1) * limit`.
4. `status` filter values → match the existing `PromptRecord.status` values: `running`, `done`, `failed`, `cancelled`.
5. `trace_id` filter → exact match only (no substring), consistent with `log_query`'s `trace` parameter.
6. `since`/`until` format → ISO 8601 string, parsed to `float` epoch via `datetime.fromisoformat()` for comparison against `started_at` (which is a `float` epoch).
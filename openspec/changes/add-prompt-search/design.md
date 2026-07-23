## Context

`PromptRegistry` (`prompt_registry.py`) tracks user-initiated agent runs with ULID prompt IDs (ADR-0013). It persists an append-only event log to `data/prompts.jsonl` and loads the entire file into memory on startup via `_replay()`. The in-memory `_records` dict grows unbounded — nothing ever removes entries. The `/prompts` Telegram command (`telegram_commands.py:cmd_prompts`) lists the last 20 records via `list_recent(20)`.

The existing event log format uses multiple lines per prompt (start, add_sub_agent, finish), making it expensive to search — each query would need to replay the full event stream to reconstruct final record state. The codebase already has a precedent for bounded JSONL storage with rotation: `JobExecutionLog` in `scheduler.py` uses a load-rotate-atomic-write pattern with `max_age_hours` and `max_per_job` caps.

In-force ADRs: ADR-0013 (ULID prompt IDs) is the only directly relevant one. No ADR needs supersession.

## Goals / Non-Goals

**Goals:**
- Cap in-memory prompt records at 100 to prevent unbounded growth.
- Enable substring search over full prompt history (in-memory + archive) with optional time-window filtering.
- Enable viewing a single prompt's full record by ID, including evicted records.
- Preserve the existing event log (`prompts.jsonl`) format and crash-safety semantics unchanged.
- Backfill the archive from existing event-log history on first startup.

**Non-Goals:**
- Semantic/embedding-based search (infrastructure exists in `tool_index.py`/`vector_utils.py` but is a different scope).
- Archive rotation or retention limits (prompt volume is realistically small; keep forever).
- Search by `trace_id` (out of scope; can be handled separately if needed).
- Storing agent responses or tool outputs in the registry (only prompt text + metadata).
- Tracking scheduled job runs in the prompt registry (scheduled runs use a separate execution log in `scheduler.py`).

## Decisions

### Decision 1: Dual-write — event log + snapshot archive

Keep `prompts.jsonl` as the append-only event log (unchanged). Add `prompts_archive.jsonl` with one self-contained snapshot line per finalized prompt, written from `finish()` via the internal `_archive_snapshot(record)` method.

**Why not replay-on-the-fly search (scan event log per query):** Every search would replay the entire event log (3+ lines per prompt), parsing 30K JSON lines for 10K prompts. A purpose-built snapshot file gives O(prompts) scanning with self-contained lines — one parse per prompt, no state accumulation.

**Why not full rewrite to snapshot format (like `JobExecutionLog`):** The event log is crash-safe: a process death between `start()` and `finish()` leaves a visible "running" record with no finish event. A snapshot format that rewrites the whole file on every mutation loses that audit trail and requires stale-record detection on replay. The dual-write keeps the audit trail AND gives cheap search.

**Archive line format** (7 fields, flat JSON object):
```jsonl
{"prompt_id":"01H...","trace_id":"r-xxx","text":"PTO request","started_at":1721835120.5,"ended_at":1721835312.8,"status":"done","sub_agent_ids":["sa-001"]}
```

### Decision 2: In-memory eviction at 100 finalized records

`start()` calls `_evict_oldest()` after adding a new record, but only when `len(self._records) > MAX_IN_MEMORY` (100). Eviction removes the oldest **finalized** record from `_records` and `_trace_to_id`. Running records are never evicted — if all 100 are running (unlikely), they all stay.

**Why 100:** Each `PromptRecord` is ~400 bytes. 100 records ≈ 40KB — negligible. The cap prevents unbounded growth over months/years while keeping the hot path (list, get, by_trace) fully in-memory.

**Why only finalized:** Running records may receive `add_sub_agent` or `finish` calls. Evicting a running record would break the supervisor's `get()` lookup and lose the in-memory trace-to-id mapping.

### Decision 3: `get()` and `by_trace()` stay in-memory-only

The sub-agent supervisor calls `get()` and `by_trace()` for active records only. Adding archive fallback would introduce unexpected file I/O on the hot path. `show()` is the explicit archive-aware lookup, used only by the `/prompts show` command.

### Decision 4: Search scans in-memory + archive, deduplicates

`search(query, days=None, limit=20)`:
1. Acquire `self._lock`, snapshot in-memory `_records` values into a local list, release lock.
2. Scan the snapshot — case-insensitive substring match on `text`, optional `started_at` time filter.
3. Stream `prompts_archive.jsonl` **without holding the lock** — same filter, skip `prompt_id`s already found in step 2.
4. Merge, sort by `started_at` descending, return top `limit`.

**Lock scope:** The lock is held only long enough to snapshot the in-memory dict reference (O(1) copy of up to 100 entries). The archive file scan happens outside the lock so `start()`, `finish()`, `get()`, and `by_trace()` are not blocked during search. This is a deliberate departure from the existing pattern where every public method holds the lock for its full body — necessary because search is the only method that does potentially long file I/O.

**Why not archive-only search:** In-memory records include the most recent prompts (up to 100) that may not yet be in the archive (running records are not archived until `finish()`). Searching in-memory first catches these.

**Why not index the archive:** Linear scan of 10K self-contained JSON lines is <50ms on any modern CPU. An index adds complexity (build, maintain, persist) for no perceptible latency gain at this scale.

### Decision 5: `show()` two-step lookup

`show(prompt_id)` calls `get()` first (in-memory, O(1)). If miss, calls `find_in_archive(prompt_id)` which streams the archive until `prompt_id` matches. Returns `None` if not found in either.

**Why not prefix matching:** ULIDs are 26 chars. The `/prompts` list renders the full ULID in a `<code>` block (copyable on Telegram mobile with long-press). Full-ULID lookup is unambiguous and simpler. Prefix matching adds ~10 lines of matching logic and ambiguity handling for marginal UX gain.

### Decision 6: Backfill on first startup

`__init__` checks if `prompts_archive.jsonl` exists. If not, calls `_backfill_archive()` which replays `prompts.jsonl` (existing `_replay` logic) and writes one snapshot line per finalized record to the archive. Running records are not archived.

**Why in `__init__`:** The replay already happens in `__init__` via `_replay()`. Backfilling immediately after replay reuses the already-built `_records` dict — no second file scan needed. After backfill, `finish()` maintains the archive going forward.

### Decision 7: Arg parsing in `cmd_prompts`

```
ctx.args = []                          → list recent (current behavior)
ctx.args[0] == "search"               → search mode
    rest = " ".join(args[1:])
    last token matching ^(\d+)([dh])$ → time window (days or hours)
    remaining tokens → query string
ctx.args[0] == "show"                 → show mode
    if len(ctx.args) < 2:              → reply "Usage: /prompts show <id>"
    args[1] → prompt_id (full ULID)
anything else                         → list recent (backward compat)
```

Time-window regex: `^(\d+)([dh])$` — `7d` → 7 days, `12h` → 12 hours. No match → no time filter (search all history).

**Empty query edge case:** If the time-window token is extracted and the remaining query is `""` (e.g., `/prompts search 7d`), `search("", days=7)` treats the empty query as a wildcard — returns all prompts within the time window. This is distinct from `/prompts` (which only lists in-memory records) — `search ""` scans the full archive too.

### Decision 8: `cmd_prompts` rendering for `show` and `search`

**`show` output** (when record found):
```
📝 Prompt <prompt_id>
Status: <icon> <status>
Trace: <trace_id>
Started: <YYYY-MM-DD HH:MM>
Ended:  <YYYY-MM-DD HH:MM> (<elapsed>)
Sub-agents: <comma-separated IDs, or "none">

Full text:
<full 200-char text, untruncated>
```

**`show` output** (when record not found):
```
❌ Prompt <prompt_id> not found.
```

**`search` result list:** Reuses the same entry format as the existing `/prompts` list (icon, status, timestamp, elapsed, sub-agent count, 80-char text preview), with a header `🔍 Search results for "<query>" (<count>)` instead of `📝 Recent Prompts`. If no results: `ℹ️ No prompts matching "<query>".`

## Risks / Trade-offs

- **[Backfill against real data]** → The backfill runs against the existing `prompts.jsonl` on first startup. If the file is large or malformed, backfill could be slow or produce an incomplete archive. **Mitigation:** backfill reuses the existing `_replay()` which already handles malformed lines gracefully (skips with warning). The archive is additive — a partial archive still enables search for the records that were written.

- **[Archive file grows forever]** → No rotation means the file grows unbounded. **Mitigation:** At ~400 bytes per line, 10K prompts = 4MB, 100K prompts = 40MB. Linear scan stays fast. If this ever becomes a problem, adding rotation is a separate change.

- **[Evicted record not in archive yet]** → A finalized record's snapshot is written to the archive in `finish()` via `_archive_snapshot()`. Eviction happens later in `start()` when a new prompt arrives and `len(_records) > MAX_IN_MEMORY`. By the time a record is evicted, its snapshot is already in the archive. **Mitigation:** The ordering is guaranteed: `finish()` writes snapshot → later `start()` evicts oldest finalized. The evicted record is always already in the archive.

- **[Concurrent search while finish() writes]** → `search()` reads the archive file while `finish()` appends to it. **Mitigation:** `search()` does not hold `self._lock` during the archive scan (see Decision 4). Append-only writes + line-by-line streaming reads are safe — a partial line at the end of the file will fail JSON parse and be skipped (same graceful handling as `_replay()`).

## Migration Plan

1. **No breaking changes** — `prompts.jsonl` format is unchanged, `get()`/`by_trace()`/`list_recent()` signatures are unchanged.
2. **First startup after upgrade** — `_backfill_archive()` creates `prompts_archive.jsonl` from existing event log. One-time cost, proportional to existing prompt count.
3. **Rollback** — Delete `prompts_archive.jsonl`. The registry falls back to event-log-only behavior (no search, no show, but list/get/by_trace work). No data loss in `prompts.jsonl`.

## Open Questions

None. All questions from the explore phase are resolved:
- MAX_IN_MEMORY = 100 — agreed.
- Archive rotation — no, keep forever.
- `/prompts show <id>` — included.
- Search by trace_id — no, out of scope.
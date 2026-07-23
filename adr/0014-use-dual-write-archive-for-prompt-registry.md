# Use dual-write archive for prompt registry search and bounded memory

## Status

Accepted

## Date

2026-07-24

## Supersedes

None

## Context

`PromptRegistry` persists an append-only event log to `data/prompts.jsonl` with multiple lines per prompt (start, add_sub_agent, finish). On startup, `_replay()` loads the entire file into an in-memory `_records` dict that grows unbounded — nothing ever removes entries. The `/prompts` command only lists the last 20 via `list_recent(20)`; there is no search or single-prompt display.

Searching the event log directly would require replaying the full event stream per query (3+ lines per prompt, stateful accumulation). Rewriting to a snapshot format (like `JobExecutionLog` in `scheduler.py`) would lose the crash-safety property of the event log: a process death between `start()` and `finish()` leaves a visible "running" record with no finish event, which a rewrite-on-every-mutation format cannot preserve without stale-record detection.

## Decision

The prompt registry SHALL use a dual-write persistence pattern:

1. **Event log** (`data/prompts.jsonl`): unchanged append-only event format, used for crash-safe replay on startup.
2. **Snapshot archive** (`data/prompts_archive.jsonl`): one self-contained JSON line per finalized prompt (7 fields: `prompt_id`, `trace_id`, `text`, `started_at`, `ended_at`, `status`, `sub_agent_ids`), written from `finish()`. Used for search and single-prompt lookup. Not rotated.
3. **In-memory cap at 100**: `start()` evicts the oldest finalized record when `len(_records) > 100`. Running records are never evicted. Evicted records remain accessible via the archive.
4. **`get()` and `by_trace()` stay in-memory-only** — no archive fallback, to avoid surprise file I/O on the sub-agent supervisor's hot path. `show()` is the explicit archive-aware lookup.
5. **`search()` snapshots in-memory records under lock, then scans the archive without holding the lock** — the only public method that does file I/O outside the lock, necessary to avoid blocking `start()`/`finish()`/`get()` during search.
6. **Backfill on first startup**: if the archive file doesn't exist, replay the event log and write finalized records to the archive. Runs once.

## Consequences

- Good, because the event log's crash-safety semantics are preserved unchanged.
- Good, because search is O(prompts) not O(events) — one JSON parse per prompt, no stateful replay.
- Good, because in-memory growth is bounded at ~40KB (100 records × ~400 bytes).
- Good, because evicted records remain accessible via `/prompts show <id>` and `/prompts search`.
- Good, because rollback is trivial — delete the archive file, no data loss in the event log.
- Bad, because two files must be maintained (event log + archive), with slightly more disk usage (snapshot duplicates finalized data already in the event log).
- Bad, because the archive file grows forever with no rotation — acceptable at realistic prompt volumes (~4MB per 10K prompts) but would need a separate change if volume becomes very large.
- Neutral, because `search()` departs from the existing lock-per-method pattern — the snapshot-then-release approach is correct but must be maintained carefully by future contributors.
# Use ULID for globally-unique prompt IDs

## Status

Accepted

## Date

2026-07-23

## Supersedes

None

## Context

Prompt IDs were monotonic integers scoped to a single process lifetime, persisted to `data/prompts.jsonl`. The operator references prompts by a single ID ("summarize actions for prompt 42") and must not need to know the internal `trace_id`. The integer ID collides when `prompts.jsonl` is deleted/reset, and a prompt running across midnight writes log records into two different daily `agent.jsonl` files — so the same `prompt_id` can refer to different runs depending on log day or registry state. The `trace_id` (`r-<8 hex>`) is globally unique but is an implementation detail the operator should not have to know.

## Decision

Prompt IDs SHALL be ULID-format strings (26-char Crockford base32, 48-bit millisecond timestamp + 80-bit random), generated inline with `secrets.token_bytes(10)` and no external dependency. The ULID is the single operator-facing handle for a run — globally unique and stable forever across restarts, registry resets, and day boundaries. The `trace_id` remains the high-cardinality join key for logs. Legacy integer IDs in `prompts.jsonl` are tolerated on replay (kept as-is in memory); no history rewrite occurs. `list_recent` sorts by `started_at` descending (not by `prompt_id` keys) to avoid `TypeError` on mixed int/str IDs.

## Consequences

- Good, because the operator can reference any historical prompt by one globally-unique ID without knowing `trace_id`.
- Good, because ULIDs are time-sortable and collision-proof (80-bit random from cryptographic RNG).
- Good, because no new runtime dependency is added — the generator is inline.
- Good, because legacy integer IDs coexist without a migration script.
- Bad, because the `prompt_id` type changes from `int` to `str` across ~8 source files and ~5 test files — a breaking change.
- Bad, because the requirement header "Prompt registry assigns monotonic prompt IDs" and the base spec Purpose remain semantically stale after archive (the no-`RENAMED` schema constraint prevents header rename); they must be reconciled manually at sync/archive.
- Neutral, because `log_query` remains active-day-only (rotated logs are not queried); global uniqueness removes the collision risk but does not make historical log lines queryable.
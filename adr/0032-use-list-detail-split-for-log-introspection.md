# Use list-detail split for log introspection

## Status

Accepted

## Date

2026-09-19

## Supersedes

None (extends the read-path contract of ADR-0025; the SQLite WAL store, its query mechanism boundary, and all of ADR-0025 remain fully in force)

## Context and Problem Statement

ADR-0025 made the full 30-day structured log store queryable via indexed SQL, but the `log_query` read path carries three budget-protective properties that together block per-failure root-cause analysis on long runs: per-field truncation at 500 chars, the per-call output budget (`agent.max_output_size`, default 4000 chars), and newest-N-only result windows with no way to enumerate the rest. A 30-minute run (~315 records) with dozens of tool failures of potentially different nature (timeout, unexpected pop-up, JavaScript error) cannot be reviewed: failure #7+ is unreachable regardless of `limit`, and each failure's distinguishing `err` text is cut mid-signature.

The budget protection cannot simply be removed — the list surface must stay cheap by design, because it is the surface the agent calls most. The architectural question is how to expose full record fidelity without re-opening the context-budget hole.

## Considered Options

- **`detail: true` flag on `log_query`**: lifts field truncation on demand — rejected: an LLM combining the flag with a wide filter degrades to one or two full records against the per-call budget (a wasted call); the flag couples list cost to detail cost.
- **`group_by` aggregation surface**: shape-of-run counts — rejected as the primary instrument: counts locate failures but do not expose per-failure nature or source (the motivating case); exact `total_matched` already provides counts via `limit: 1` probes.
- **Raise default `max_output_size` / `limit`**: brute-force budget increase — rejected: cost scales linearly with run length and defeats the context budget's purpose.
- **Offset / cursor-token paging** (as the enumeration mechanism): position-based pages — rejected: O(n²) scan-and-discard on the store, corrupts under concurrent appends (new rows shift the window mid-run), and requires cursor state (TTLs, invalidation); subsumed into the chosen option as the id-anchored walk instead.
- **List-detail tool split with id-anchored enumeration** (`log_query` gains record `id` + `before_id` backward walk; new `log_detail` returns one record at full fidelity, hard-capped, with an oversized-dump escape hatch): each tier gets its own budget contract and full fidelity becomes an explicit per-record decision — chosen.

## Decision Outcome

Chosen: log introspection becomes a two-tier read contract over the unchanged store. The **list tier** (`log_query`) stays bounded: every returned record carries its immutable SQLite row `id`, and the optional `before_id` filter (`WHERE id < ?`; the page query is always ordered by `id DESC` — including a walk's first page and results later trimmed by the per-call output size, a post-SQL trim unknowable at query-build time — so consecutive pages cannot skip records when emit-time `ts` order and drain-order `id` diverge under concurrent producers; `total_matched` always reports the entire match set, not the page — a set-size signal, never a completion signal, since the whole-set count grows while the store appends) walks the match set newest→oldest without any cursor state — the anchor is the last id seen, a value already present in the previous result. Because pages anchor to immutable ids rather than positions, a walk stays correct while the store is still appending (mid-run self-research); walk completion is exhaustion-based — a page that returns no records ends the walk. The **detail tier** (`log_detail`) takes a single required `id` and returns that record at full fidelity (per-field truncation lifted), subject to a 65536-char hard cap on the serialized record; oversized records dump to `~/.<agent>/results/<trace-id>/` — the established oversized-artifact destination (Tier 1, jail-mounted rw, `file_read`-accessible without confirmation; the record's stored trace is validated against the trace-id format before use as a path component, falling back to the current run's trace) — with the artifact path plus a truncated preview returned, preserving ingestion-time vault redaction. The two tiers share one record projection (`row_to_record`), so a detail record is identity-equal to its list preview, only untruncated.

## Consequences

- Good, because per-failure review of long runs becomes possible within a bounded context budget: list → classify → drill is a per-record decision the agent controls.
- Good, because the walk needs no cursor tokens, TTLs, or invalidation: the anchor is a value already in the previous result.
- Good, because mid-run self-research on a still-appending store is safe: id-anchored pages cannot skip or duplicate records, unlike position-based paging.
- Bad, because the agent pays a round trip per drilled record; batching was deliberately rejected to preserve the explicit per-record decision.
- Bad, because a malformed `before_id` silently re-reads the newest page instead of advancing (unlike `limit`, omission is not harmless) — walk completion is exhaustion-based (an empty page ends the walk; `total_matched` is a set-size signal that grows on a live store, never a completion signal), and the handler validates that every returned id is below the anchor so an unapplied anchor surfaces instead of masquerading as progress.
- Neutral, because the `id` field becomes part of the record contract returned to the LLM (additive; existing consumers unaffected).
- Follow-up: the two-tier pattern (bounded id-referenced list + per-record drill with dump escape hatch) is the template for any future store-backed introspection surface.
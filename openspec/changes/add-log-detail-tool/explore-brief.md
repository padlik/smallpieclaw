# Explore brief — add-log-detail-tool

## Context / problem

During long-running tasks (30+ min, ~315 structured records) the agent cannot
perform per-failure root-cause analysis with `log_query` alone. Three verified
blockers (code facts, `builtin_tools/logquery_helpers.py`, `builtin_tools/secrets_log.py`):

1. Per-field 500-char truncation (`_LOG_QUERY_FIELD_MAXLEN`) — a browser failure's
   `err` (pop-up HTML, JS stack trace) is cut mid-signature; timeout vs pop-up vs
   JS error are indistinguishable.
2. Per-call 4000-char payload cap (`agent.max_output_size`) — only the newest
   handful of failure records fit per call.
3. No enumeration mechanism — the tool always returns "newest N that fit";
   failure #7+ is unreachable no matter how large `limit` is (limit alone does
   not help; raising it beyond the size cap returns nothing more).

`total_matched` is already exact across the 30-day window (COUNT(*) on the
indexed WHERE), so the size of a prompt's record set is knowable cheaply.

## Agreed approach (converged with user across the conversation)

List-detail split, reusing the established submit/list → drill/fetch pattern
(`spawn_agent`/`get_agent_result` precedent):

### `log_query` changes (additive, defaults untouched)
- Expose `id` (SQLite row PK) in every returned record (`row_to_record`).
- Add optional `before_id` (int): `WHERE id < ?` — walks the match set
  newest→oldest. A "synthetic cursor" (user's term): no cursor state, no TTLs,
  correct while the store is still appending (mid-run self-research), tie-proof
  vs ts, PK range scan (no OFFSET scan-and-discard).
- Composes with all existing filters; `total_matched` stays exact so the agent
  knows when the walk is complete against a known set size.
- Descriptor text updated: `limit` × `max_output` interplay (raising `limit`
  alone doesn't return more records); COUNT-probe pattern (`limit: 1` = exact
  count query); walk-then-drill pattern.

### New `log_detail` tool
- Args: `{id: int}` (required). Read-only, not confirmation-capable.
- Returns the full record — per-field 500-char cap lifted — with a hard
  record-level size cap (value: open question, see below).
- Record serialized via the same shape `row_to_record` produces (wide columns +
  parsed `extra` merged at top level).
- Oversized record (exceeds hard cap): full record dumped to a file, tool result
  returns pointer + truncated preview (location: open question, see below).
- Not-found id → well-formed EMPTY result (mirrors `log_query` EMPTY
  convention; no error).
- Missing/unreadable store → well-formed EMPTY result.

### Workflow this enables (the motivating scenario)
```
log_query    {"event_type": "TOOL_FAILED", "prompt_id": P, "limit": 20}   → ids + 500-char previews
log_detail   {"id": 3108}                                                → full err text
log_detail   {"id": 3105}
log_query    {"event_type": "TOOL_FAILED", "prompt_id": P, "before_id": X, "limit": 20}  → next page
```
Failure classification by nature/source (timeout / unexpected pop-up / JS error)
becomes possible because the distinguishing `err` text is fully readable and
every failure is visitable.

## Rejected alternatives (from the discussion)

- `group_by` aggregation as primary instrument — user rejected: counts locate
  failures but don't expose per-failure nature/source; every failure can have a
  different root cause. (Aggregation dropped from scope; COUNT-probe via
  `total_matched` covers triage.)
- `detail: true` flag on `log_query` — rejected in favor of a separate tool:
  an LLM combining `detail` with a wide filter degrades to 1–2 full records
  against the 4000-char cap (wasted call); a separate tool makes full fidelity
  an explicit per-record decision, enforcing list → classify → drill.
- Offset/cursor-token paging — rejected: `before_id` achieves the same walk with
  zero new state (immutable PK anchor), no TTL/invalidation machinery, and
  correct behavior on a live (mid-run, still-appending) store.
- Raising `max_output_size` / `limit` as the fix — rejected: doesn't scale,
  misdiagnosed in the original note (size cap binds, not limit).

## Full parameter surface after the change

`log_query`: `trace`, `level`, `event_type`, `tool`, `since`, `limit`,
`text`/`query`, `prompt_id`, **`before_id`** (new). Returned records gain
**`id`** (new field).

`log_detail` (new tool): `id` (required).

## Cross-module data flows (who calls who)

- `builtin_executor.py` dispatch table: add `"log_detail"` route →
  `LogQueryTools._exec_log_detail` (new handler beside `_exec_log_query`, same
  `LogQueryTools` class in `builtin_tools/secrets_log.py`).
- Both tools read via `sqlite_log.connect_store()` + `row_to_record()`;
  `log_detail` needs `row_to_record` to expose `id` (one addition there, or
  handler adds it — decided in design).
- Registry: `builtin_tools/descriptors.py` `BUILTIN_TOOLS` + `builtin_tools/schemas.py`
  `BUILTIN_TOOL_SCHEMAS` entries for `log_detail`; `log_query` descriptor/schema
  updated (`before_id` param + caps documentation).
- Oversized dump: reuse `_finalize_shell_log`'s relocation pattern →
  `results/<trace-id>/` (subject to open question below), with the same
  vault-secret redaction concern noted (records are already redacted at
  ingestion by `agent_logging`, so dump redaction is a defense-in-depth
  question for design).
- Prompt docs: `prompts/system/03-capabilities.md` +
  `prompts/sub-agent/03-tools.md` tool lists.
- Tests: `tests/test_log_query.py` (id, before_id walk) + new detail tests;
  dispatch routing test list; prompt-loader test tool lists.

## Affected specs (capability mapping)

- `runtime-log-introspection` (MODIFIED): id exposure, `before_id` walk,
  enumeration-during-live-run scenario, per-failure full-fidelity access via
  `log_detail`, dump-to-file escape hatch.
- `builtin-tool-execution` (MODIFIED): `log_detail` registration requirement
  (pattern: `context_profile` requirement, tool-count rule update, not
  confirmation-capable).

## Open questions — RESOLVED (user confirmed 2026-09-19)

1. **Oversized-record dump location** — **`results/<trace-id>/`** (user
   confirmed). Follows agent-results-dir spec: Tier 1 rw, jail-mounted,
   `file_read` without confirmation, same destination as oversized shell
   artifacts. "Temp folder" idea dropped in favor of the codebase convention.
2. **Hard cap value** — **64KB (65536 chars)** per serialized record (user
   confirmed). Far above any real record; dump path is the rare escape hatch.
3. **`log_detail` arity** — **single `id` only** (user confirmed). Explicitness
   is the point of the split.

## Notes

- No breaking changes: all existing `log_query` defaults, Option C view, trace
  auto-widening, exact `total_matched`, truncation reporting stay untouched.
- `vulture_whitelist.py` may need updating if new public symbols appear.
- Existing test fixture `make_builtin_executor(log_store_path=...)` supports
  both tools out of the box.
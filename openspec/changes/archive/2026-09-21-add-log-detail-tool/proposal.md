## Why

The agent cannot currently perform per-failure root-cause analysis on its own
runs. Three verified blockers in the `log_query` read path make every failure's
full text unreachable: (1) per-field truncation at 500 chars
(`_LOG_QUERY_FIELD_MAXLEN`) cuts a failure's `err` mid-signature — timeout vs
unexpected pop-up vs JS error are indistinguishable; (2) the per-call output
budget (`agent.max_output_size`, default 4000 chars) admits only the newest
handful of matching records; (3) there is no way to enumerate the rest — the
tool always returns "the newest N that fit", so failure #7+ is unreachable
regardless of `limit`. Long-running tasks (30+ min, ~315 records, dozens of
tool failures of potentially different nature) are exactly where this hurts:
self-research and review require visiting every failure and reading its full
text.

## What Changes

- `log_query` returns the SQLite row `id` in every record (new top-level
  `id` field), giving each log record a stable reference for follow-up reads.
- `log_query` gains an optional `before_id` parameter (`WHERE id < ?`): walks
  the match set newest→oldest, composes with all existing filters, and is
  correct while the store is still appending (mid-run self-research), because
  pages anchor to immutable ids rather than to positions. No cursor state, no
  TTLs — the anchor is the id of the last record seen in the previous page.
  A walk completes when a page returns no records (exhaustion-based);
  `total_matched` reports the whole-set size, which may keep growing on a
  live store, so it is not a completion signal. The page query is always
  ordered by `id` so the walk cannot skip records even when emit-time `ts`
  and drain-order `id` diverge (concurrent producers) — the ordering applies
  whether the result spans multiple `limit`-pages or is trimmed by the
  per-call output size, which is applied after the SQL sort and cannot be
  known at query-build time.
- New built-in tool `log_detail` (`{id: int}` required): returns one full log
  record with the 500-char per-field cap lifted, subject to a hard cap of
  65536 chars on the serialized record. Read-only, not confirmation-capable.
  Not-found id or unreadable store → well-formed empty result, mirroring
  `log_query`'s EMPTY convention.
- The list-detail split is deliberate: a `detail: true` flag on `log_query`
  would let a wide filter degrade to one or two full records against the
  per-call output budget (a wasted call). A separate tool makes full fidelity
  an explicit per-record decision — list, classify, then drill — following
  the `spawn_agent`/`get_agent_result` submit-then-fetch precedent.
- Oversized records (serialized record > 65536 chars): the full record is
  dumped to `~/.<agent>/results/<trace-id>/` (the established
  oversized-artifact destination mandated by the `agent-results-dir` spec:
  Tier 1, jail-mounted rw, `file_read`-accessible without confirmation),
  reusing the `_finalize_shell_log` relocation pattern. The trace-id used for
  the subdirectory is the record's own `trace` **validated against the
  `r-[0-9a-f]{8}` trace-id format** (stored record content is untrusted as a
  path component); records with a non-conforming or missing trace fall back
  to the current run's trace, and the resolved dump path is asserted to stay
  within `results_dir`. Records are already vault-redacted at ingestion
  (`agent_logging`); whether the dump path adds defense-in-depth redaction is
  resolved in design (it must not weaken existing redaction). The tool result
  returns the file pointer plus a truncated preview.
- `log_query` tool descriptor updated to teach the effective usage patterns:
  the `limit` × `max_output` interplay (raising `limit` alone does not return
  more records when the size cap binds), the COUNT-probe pattern (`limit: 1`
  yields an exact `total_matched` count), and the walk-then-drill pattern
  (`before_id` paging + `log_detail` for per-failure inspection).
- All existing `log_query` behavior is preserved (defaults, Option C
  high-signal view, trace auto-widening, exact `total_matched`, truncation
  reporting) with one refinement: **the page query is always ordered by
  `id`** instead of emit-time `ts`, so that consecutive `before_id` pages
  cannot skip records when concurrent producers (main + sub-agents +
  scheduler) make `ts` order and drain-order `id` diverge. The ordering must
  not depend on which truncation path fires: the per-call output-size trim
  runs after the SQL sort and is unknowable at query-build time, so gating
  the ordering on count-truncation leaves the size-trim path (the verbose
  failure workload: a handful of large records under `limit`, trimmed by
  `max_output`) gappable. Since emit-vs-drain skew is sub-second, the two
  orders are indistinguishable for results that fit one page — nothing
  observable is lost. No breaking changes.

## Capabilities

### New Capabilities
(none)

### Modified Capabilities
- `runtime-log-introspection`: the `log_query` contract gains record `id`
  exposure, `before_id` backward enumeration (including correctness on a
  still-appending store), and a companion `log_detail` tool for per-record
  full-fidelity reads with an oversized-record dump-to-file escape hatch.
- `builtin-tool-execution`: `log_detail` is registered as a built-in tool
  (routed by name, not confirmation-capable), extending the built-in set
  alongside `context_profile`-style registration requirements.

## Impact

- `builtin_tools/secrets_log.py` — new `_exec_log_detail` handler beside
  `_exec_log_query`; dump-to-file path for oversized records, reusing the
  `_finalize_shell_log` relocation pattern (design decides redaction
  defense-in-depth for the dump).
- `builtin_tools/logquery_helpers.py` — `before_id` WHERE-clause compilation;
  record `id` projection.
- `sqlite_log.py` — `row_to_record` exposes the `id` PK (implementation may
  add it in the handlers instead; decided in design).
- `builtin_tools/descriptors.py` + `builtin_tools/schemas.py` — `log_detail`
  registry entries; `log_query` schema/descriptor updates (`before_id`, caps
  documentation).
- `builtin_executor.py` — dispatch-table entry for `log_detail`.
- `prompts/system/03-capabilities.md`, `prompts/sub-agent/03-tools.md` — tool
  documentation.
- `tests/test_log_query.py` (id, before_id), new `log_detail` tests
  (full-fidelity read, not-found, oversized dump), dispatch routing test list,
  prompt-loader tool lists.
- `vulture_whitelist.py` — any new public API symbols flagged as unused.
- No config, dependency, or schema changes to the SQLite store itself (the
  `id` PK already exists; no new columns or indexes are required — `WHERE
  id < ?` is a PK range scan).
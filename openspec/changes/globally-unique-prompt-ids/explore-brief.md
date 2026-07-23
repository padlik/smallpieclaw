# Explore Brief: Globally-Unique Prompt IDs

## Problem (from operator feedback + review)

1. **`/prompts` is unrecognizable.** It shows only `Prompt #N`, status icon, elapsed time, and sub-agent count. No prompt text, no start date, no trace. An operator seeing "Prompt #42" cannot tell which prompt it was.
2. **Operator references prompts by a single ID.** "Summarize actions for prompt 42" must work without the operator knowing the internal `trace_id`. The prompt ID must be the single operator-facing handle — which means it must be globally unique and resolvable to the full record.
3. **Prompts span days.** A prompt started near midnight writes log records into two different daily `agent.jsonl` files. A prompt ID that is only unique within a day (or within a process lifetime) is insufficient for cross-run analysis.

## Rejected alternatives

- **Doc-only fix (keep sequential int, clarify scope).** Rejected by operator: the operator wants a single ID that works for historical analysis; forcing `trace_id` leaks implementation and the sequential int collides on registry reset and across days.
- **Full 36-char UUID.** Rejected: too long/ugly for Telegram display and operator reference.
- **Date-prefixed sequential (e.g., `2025-07-23-0001`).** Rejected: still collides on registry reset within a day, and the date prefix is redundant once the start timestamp is shown in `/prompts`.

## Chosen approach

**Short globally-unique ID + recognizable `/prompts` listing.**

- Replace the monotonic integer `prompt_id` with a short unique token. Use a **ULID** (26-char Crockford base32, time-sortable, 48-bit ms timestamp + 80-bit random). ULIDs sort lexicographically by creation time, so `list_recent` ordering is natural, and collisions are effectively impossible (80 bits of entropy).
- Keep the operator-facing label as "Prompt `<id>`" (the ULID is the handle). No separate sequential number.
- Make `/prompts` show: prompt ID (short form), start timestamp, truncated text, status, elapsed, sub-agent count — so the operator can recognize prompts.
- `log_query`'s `prompt_id` filter continues to match the `prompt_id` field in log records; now that field is globally unique, cross-day queries are unambiguous (the filter still only reads the active day's log, but the ID itself never collides with a different run).

## Mapping: ID format

| Field | Before | After |
|---|---|---|
| `PromptRecord.prompt_id` type | `int` | `str` (ULID) |
| Generation | `self._next_id += 1` | `ulid.new()` (or `ulid-py` lib) |
| Persistence key in JSONL | `"prompt_id": <int>` | `"prompt_id": "<ulid>"` |
| Log context `prompt_id` | `str(int)` | `str` (ULID, already str) |
| Telegram display | `Prompt #42` | `Prompt 01J...` (full ULID) |
| `log_query` filter compare | `str(rec) != str(arg)` | unchanged (both str) |
| `_trace_to_id` value | `int` | `str` |

## Cross-module data flow

- `telegram_interface.py` → `PromptRegistry.start(trace_id, text)` returns `PromptRecord` with ULID `prompt_id`.
- `prompt_id` (str) → `AgentController.run(task, prompt_id=...)` → `bind_run_context(prompt_id=str)` → written into `agent.jsonl`.
- `prompt_id` (str) → `SubAgentSupervisor` options → `SubAgentRecord.prompt_id`.
- `builtin_tools/agents.py` reads `_current_prompt_id` (now str) → `registry.add_sub_agent(prompt_id, agent_id)`.
- `log_query` filter: `prompt_id` arg matched as string against log record `prompt_id` field.
- `telegram_commands.cmd_prompts` → `registry.list_recent()` → renders ID + text + date + status + elapsed + sub-agents.

## Open questions

1. **ULID library dependency.** Add `ulid-py` to requirements, or inline a small generator (timestamp_ms + secrets.token_hex)? Inline avoids a new dep and keeps the format under our control. **Decision: inline generator** — 6 bytes timestamp (ms) + 10 bytes random, Crockford base32 encode. No new dependency, no install approval needed.
2. **Migration of existing `prompts.jsonl`.** Old records have int `prompt_id`; new records have str ULID. Replay must handle both. **Decision: replay tolerates int IDs (keeps them as-is in memory); only new records get ULIDs.** No rewrite of history. `list_recent` may show a mix until old records age out.
3. **`_next_id` counter.** Removed — ULIDs are generated, not sequenced. Replay no longer needs `max_id`.
4. **Short display form.** Full 26-char ULID in Telegram is long but unambiguous. Could show first 8 chars with full on demand. **Decision: show full ULID** — truncation risks ambiguity and the operator needs to copy-paste it for log queries.

## Affected specs

- `prompt-tracking` (MODIFIED): ID generation changes from monotonic int to ULID; `/prompts` listing adds text + timestamp.
- `runtime-log-introspection` (MODIFIED): `prompt_id` filter now matches a ULID string; scenarios updated to use ULID examples.
- `telegram-command-surface` (unchanged): `/prompts` discovery/registration unaffected; content contract stays in `prompt-tracking`.
## Why

Prompt IDs are sequential integers scoped to a single process lifetime. They collide when `data/prompts.jsonl` is deleted/reset, and a prompt that runs across midnight writes log records into two different daily `agent.jsonl` files — so the same `prompt_id` can refer to different runs depending on which log day is active. The operator is expected to reference prompts by a single ID ("summarize actions for prompt 42"), but the current ID is neither globally unique nor recognizable: the `/prompts` command shows only `Prompt #N`, status, elapsed, and sub-agent count — no text, no date, no trace — so the operator cannot tell which prompt is which. The internal `trace_id` is globally unique but is an implementation detail the operator should not have to know.

## What Changes

- **BREAKING**: Replace the monotonic integer `prompt_id` with a short globally-unique string ID (ULID format: 26-char Crockford base32, time-sortable, generated inline with no new dependency). The ID is stable forever — across restarts, registry resets, and day boundaries.
- **BREAKING**: `PromptRecord.prompt_id` type changes from `int` to `str`. All call sites that pass, store, compare, or display `prompt_id` are updated.
- The `/prompts` Telegram command now shows the prompt ID, start timestamp, truncated prompt text, status, elapsed time, and sub-agent count — so the operator can recognize prompts without relying on the ID alone.
- `log_query`'s `prompt_id` filter continues to match the `prompt_id` field in log records; the field is now globally unique so cross-day analysis is unambiguous.
- Existing `prompts.jsonl` records with integer IDs are tolerated on replay (kept as-is in memory); only new records receive ULIDs. No history rewrite.

## Capabilities

### New Capabilities
<!-- None — this change modifies existing capabilities. -->

### Modified Capabilities
- `prompt-tracking`: Prompt ID generation changes from a monotonic integer to a globally-unique ULID string; the `/prompts` listing adds start timestamp and truncated prompt text so prompts are recognizable.
- `runtime-log-introspection`: The `log_query` `prompt_id` filter now matches a globally-unique ULID string instead of an integer; scenarios updated to reflect ULID values and cross-day unambiguous matching.

## Impact

- **`prompt_registry.py`**: ID generation (`_next_id` counter removed, ULID generator added), `PromptRecord.prompt_id` type `int`→`str`, replay tolerates legacy int IDs, `_trace_to_id` value type `int`→`str`.
- **`telegram_commands.py`**: `cmd_prompts` rendering adds timestamp + text; ID display format changes.
- **`agent_controller.py`**: `run(task, prompt_id)` parameter type `Optional[int]`→`Optional[str]`; `bind_run_context` call unchanged (already takes str).
- **`sub_agent_supervisor.py`**: `SupervisionOptions.prompt_id` and `SubAgentSupervisor` field types `int`→`str`.
- **`sub_agent_registry.py`**: `SubAgentRecord.prompt_id` type `Optional[int]`→`Optional[str]`.
- **`builtin_executor.py`**: `_current_prompt_id` type `Optional[int]`→`Optional[str]`.
- **`builtin_tools/agents.py`**: reads/propagates `prompt_id` as str.
- **`builtin_tools/schemas.py`**: `log_query` `prompt_id` parameter description updated.
- **`telegram_interface.py`**: passes `prompt_record.prompt_id` (now str) through.
- **Tests**: `test_prompt_registry.py`, `test_log_query_prompt_id.py`, `test_supervisor_prompt_wiring.py`, `test_prompt_id_logging.py`, `test_prompts_command.py` updated for str IDs and new `/prompts` fields.
- **No new runtime dependencies** — ULID generation is inlined (timestamp_ms + `secrets.token_hex`, Crockford base32 encode).
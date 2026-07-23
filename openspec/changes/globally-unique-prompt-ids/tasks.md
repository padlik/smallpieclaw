## 1. ULID generator

- [x] 1.1 Add an inline ULID generator function (6-byte ms timestamp + `secrets.token_bytes(10)` = 16 bytes, Crockford base32 encoded to 26 chars) to `prompt_registry.py`. No external dependency.
- [x] 1.2 Add a unit test for the generator: output is 26 chars, Crockford base32 charset, timestamp prefix increases across milliseconds, two calls in the same ms differ in the random suffix.

## 2. PromptRegistry core changes

- [x] 2.1 Change `PromptRecord.prompt_id` type from `int` to `str`; update `self._records: dict[int, PromptRecord]` → `dict[str, PromptRecord]` annotation.
- [x] 2.2 Remove the `_next_id` counter; `start()` calls the ULID generator instead of incrementing.
- [x] 2.3 Change `_trace_to_id` value type from `int` to `str`.
- [x] 2.4 Update `_replay()` to tolerate both legacy `int` and new `str` `prompt_id` values from `prompts.jsonl` (accept any; store as-is in `_records` and `_trace_to_id`). Remove the `max_id` logic and the `isinstance(prompt_id, int)` skip guard.
- [x] 2.5 Change `list_recent()` to sort by `started_at` descending (not by `prompt_id` keys) to avoid `TypeError` on mixed int/str IDs.
- [x] 2.6 Update `finish()`, `add_sub_agent()`, `get()` signatures/annotations: `prompt_id: int` → `prompt_id: str`.
- [x] 2.7 Change all `%d` format specifiers for `prompt_id` to `%s` in the five `logger.*` calls (lines 152, 165, 177, 184, 193) — `%d` raises `TypeError` on a ULID string and Python logging silently swallows it, so `make test` will not catch it.
- [x] 2.8 Update the module docstring (lines 4–5), class docstring (line 42), and `_replay` docstring (lines 67–70, remove the `max(prompt_id) + 1` sentence) to reflect globally-unique ULID IDs.

## 3. PromptRegistry tests

- [x] 3.1 Update `tests/test_prompt_registry.py`: IDs are now ULID strings, not sequential ints (remove `assert r1.prompt_id == 1` etc.; assert 26-char ULID format). Add `caplog` assertions on `start()`/`finish()` log output (`getMessage()` forces lazy `%`-formatting and would raise if `%d` were reintroduced — a regression guard for task 2.7).
- [x] 3.2 Add a test: `list_recent` sorts by `started_at` descending, not by ID.
- [x] 3.3 Add a test: mixed legacy-int + ULID-str IDs in `_records` do not raise `TypeError` in `list_recent`.
- [x] 3.4 Add a test: `_replay` tolerates a `prompts.jsonl` with legacy int IDs and new ULID IDs side-by-side.
- [x] 3.5 Add a test: prompt ID survives registry reset (delete `prompts.jsonl`, new prompt gets a different ULID, no collision).

## 4. Type propagation through call sites

- [x] 4.1 `agent_controller.py`: `run(task, prompt_id: Optional[int])` → `Optional[str]`; `bind_run_context` call already stringifies — pass the ULID directly.
- [x] 4.2 `sub_agent_supervisor.py`: `SupervisionOptions.prompt_id` and field types `int` → `str`.
- [x] 4.3 `sub_agent_registry.py`: `SubAgentRecord.prompt_id: Optional[int]` → `Optional[str]`.
- [x] 4.4 `builtin_executor.py`: `_current_prompt_id: Optional[int]` → `Optional[str]`.
- [x] 4.5 `builtin_tools/agents.py`: reads/propagates `prompt_id` as str (update any int assumptions).
- [x] 4.6 `telegram_interface.py`: passes `prompt_record.prompt_id` (now str) through unchanged.

## 5. Type-propagation tests

- [x] 5.1 Update `tests/test_supervisor_prompt_wiring.py`: `prompt_id` values are ULID strings (e.g., `"01JARYN6R0..."`), not ints.
- [x] 5.2 Update `tests/test_prompt_id_logging.py`: `bind_run_context` receives the ULID string; assert `kwargs.get("prompt_id")` equals the ULID.
- [x] 5.3 Update `tests/test_agent_runtime_characterization.py`: `_fake_agent_run(_task, prompt_id=None)` signature unchanged but any passed values are strings.
- [x] 5.4 Update `tests/test_prompt_approval_ttl.py`: replace int literals (e.g., `executor._current_prompt_id = 10`) with ULID strings to match the new str contract.

## 6. /prompts command rendering

- [x] 6.1 Update `telegram_commands.cmd_prompts`: each entry shows full ULID (no truncation), start timestamp, truncated prompt text (~80 chars), status icon, elapsed, sub-agent count.
- [x] 6.2 Update `tests/test_prompts_command.py`: assert the rendered output includes timestamp, text, and the full ULID.

## 7. log_query schema and filtering

- [x] 7.1 Update `builtin_tools/schemas.py`: `log_query` `prompt_id` parameter description to note it is a globally-unique ULID string.
- [x] 7.2 Verify `builtin_tools/secrets_log.py` `prompt_id` filter: `str(rec_prompt_id) != str(prompt_id_arg)` already works for str IDs — no logic change, but confirm with a ULID value.
- [x] 7.3 Update `tests/test_log_query_prompt_id.py`: use ULID string values in test fixtures instead of ints.

## 8. Lint, validate, and verify

- [x] 8.1 Run `ruff check .` and `vulture . vulture_whitelist.py --min-confidence 80` — fix any new findings.
- [x] 8.2 Run `make test` — all tests pass.
- [x] 8.3 Run `openspec validate globally-unique-prompt-ids --type change --strict` — change artifacts validate.
- [x] 8.4 Update `vulture_whitelist.py` if any new public symbols are flagged.
## 1. Re-home vision routing onto all-models scan

- [x] 1.1 In `llm_client.py` `_run_with_fallback()`, replace the fallback-candidates vision filter (`vision_candidates = [i for i in candidates if ...]`) with an all-models scan: `vision_models = [i for i, m in enumerate(self._models) if m.get("vision")]`. Pick first by config order.
- [x] 1.2 Wrap the vision model switch in a try/finally so `_active_idx` is restored to `primary_idx` after the image request completes (success or error). Emit the existing progress message ("⚠️ Active model is not vision-capable; switching to vision model '…'") when switching.
- [x] 1.3 Preserve the `LLMPermanentError` when no vision-capable model is configured — keep the existing error message ("no vision-capable model is configured").
- [x] 1.4 Rework `tests/test_p2_vision_fallback.py` to test the all-models scan + revert behavior: (a) image routes to first vision model by config order; (b) primary restored after request; (c) no-vision-model error preserved; (d) active model already vision-capable → no switch. Remove any fallback-list-dependent test setup.
- [x] 1.5 Run `make check` (lint + test) and confirm green.

## 2. Remove fallback_models from LLMClient

- [x] 2.1 In `llm_client.py` `__init__`, remove the `fallback_models` constructor param and the `_fallback_indices` resolution loop (lines ~142-154). Keep `AgentConfig.fallback_models` parsing in `config_schema.py` but add a deprecation warning when the field is non-empty.
- [x] 2.2 In `llm_client.py` `_run_with_fallback()`, simplify the candidates loop to `[primary_idx]` only (no fallback chain). Remove the `for seq, idx in enumerate(candidates)` fallback iteration; keep the single-model call path. Method names `chat_with_fallback` / `chat_with_tools_fallback` stay as single-model no-op-fallback.
- [x] 2.3 Remove `fallback_models` from `RuntimeOptions` in `agent_runtime.py` and the trichotomy (None/[]/list) inheritance logic. Update `AgentRuntime.create` and the `ReactContext` construction to not thread it.
- [x] 2.4 Remove `fallback_models` from `agent_controller.py` sub-agent factory kwargs and `execution_plan.py` plan-step factory kwargs.
- [x] 2.5 In `main.py`, remove `fallback_models` threading from sub-agent spawn paths (lines ~615, ~635).

## 3. Remove fallback_models from scheduler and tools

- [x] 3.1 In `scheduler.py`, remove `fallback_models` from `add_job()` params, `_jobs_meta` storage, and the toml persistence/read path (lines ~423-445, ~580, ~694-744, ~1089-1095). Add a deprecation warning when loading a `scheduler.toml` job that contains `fallback_models` — drop the key from meta and do not pass it to `spawn_args`.
- [x] 3.2 In `sub_agent_supervisor.py`, remove the `fallback_models` inherited-fallback logging (lines ~133-134).
- [x] 3.3 In `builtin_tools/agents.py`, remove `fallback_models` from the `spawn_agent` tool arg parsing (line ~236, ~267).
- [x] 3.4 In `builtin_tools/schemas.py` and `builtin_tools/descriptors.py`, remove the `fallback_models` schema entry and descriptor text (schemas.py:359, descriptors.py:73).
- [x] 3.5 In `builtin_tools/schedule.py`, remove `fallback_models` pass-through to `scheduler.add_job` (line ~82).
- [x] 3.6 In `telegram_formatter.py`, remove the `fallback_models` job-list display (lines ~187-190).

## 4. Delete fallback tests and update existing tests

- [x] 4.1 Delete `tests/test_scheduler_fallback.py` (entire file — 36 references, dedicated to the removed feature).
- [x] 4.2 Update `tests/test_agent_runtime_skeleton.py` to remove `fallback_models` trichotomy tests (lines ~61, ~117, ~133, ~151, ~164, ~176-182, ~345-361). Ensure a fallback-free positive test for model override remains (the "Model override is preserved" scenario from the MODIFIED spec).
- [x] 4.3 Update `tests/test_agent_runtime_characterization.py` to remove `fallback_models` from runner construction (lines ~93, ~97, ~116, ~327-337).
- [x] 4.4 Update `tests/execution_harness.py` to remove the `fallback_models` param from the config builder (line ~112).
- [x] 4.5 Update `tests/test_sub_agent_supervisor.py`, `tests/test_ollama_history_tool_args.py`, `tests/test_native_tool_args_guard.py` to remove `fallback_models` references.
- [x] 4.6 Add test: single-model error propagation — a transient error on the primary model propagates to the caller with NO fallback attempt (guards against partial fallback logic left in `_run_with_fallback()`).
- [x] 4.7 Run `make check` and confirm green after fallback removal.

## 5. Add per-model context_window config field

- [x] 5.1 In `config_schema.py` `ModelConfig`, add `context_window: int | None = None` field. Update `_parse_model()` to read `context_window` from the config entry (line ~545-551).
- [x] 5.2 In `context_manager.py` `maybe_compact()`, change the threshold formula from `int(ctx_max_tokens * 0.85)` to `int((ctx_max_tokens - model_max_tokens) * 0.85)`. The `model_max_tokens` comes from the active model config (passed in or read via `llm.llm_cfg`).
- [x] 5.3 In `react_loop.py` at the `maybe_compact()` call site (line ~1164), compute `effective = ctx.llm.llm_cfg.get("context_window") or ctx.ctx_max_tokens` and pass `effective` as `ctx_max_tokens`. Pass `ctx.llm.llm_cfg.get("max_tokens") or 1024` as the completion budget (the `or` guard handles explicit `None` values that `.get(key, default)` would return).
- [x] 5.4 Update `context_manager.py` docstring to document the new formula: threshold reserves completion tokens first, then applies 85% margin.

## 6. Update vulture whitelist and config examples

- [x] 6.1 Update `vulture_whitelist.py`: add `ModelConfig.context_window` to the whitelist; remove any stale `fallback_models` / `_fallback_indices` entries.
- [x] 6.2 Update `config.toml.example`: add `context_window` field to a `[[models]]` entry with a comment explaining it's optional (falls back to `agent.ctx_max_tokens`). Add deprecation note for `agent.fallback_models`.
- [x] 6.3 Update `scheduler.toml.example`: remove the per-job `fallback_models` field (lines ~25-26). Add a comment noting the field is deprecated and ignored if present.

## 7. Tests for per-model context window

- [x] 7.1 Add test: model with `context_window` set uses per-model limit for compaction threshold.
- [x] 7.2 Add test: model without `context_window` falls back to `agent.ctx_max_tokens`.
- [x] 7.3 Add test: compaction threshold reserves completion tokens — `max(int((8192 - 1024) * 0.85), 256)` = 6092, not `int(8192 * 0.85)` = 6963.
- [x] 7.4 Add test: deprecation warning logged when `agent.fallback_models` is non-empty in config.
- [x] 7.5 Add test: deprecation warning logged when loading a `scheduler.toml` job with `fallback_models`; key dropped from meta.
- [x] 7.6 Run `make check` and confirm green.

## 8. Docs and validation

- [x] 8.1 Update README: document per-model `context_window` field; remove `fallback_models` references; note vision auto-switch works with any configured vision model.
- [x] 8.2 Run `openspec validate per-model-context-window --type change --strict` and confirm it passes.
- [x] 8.3 Run `make check` (lint + test) final pass — confirm 1627 passed, 1 skipped baseline holds (or updates coherently with new tests).

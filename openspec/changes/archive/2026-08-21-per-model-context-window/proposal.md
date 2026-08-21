## Why

The agent's context-size limit (`ctx_max_tokens`, default 90,000) is a single static value decoupled from the actual model's context window. With `fallback_models`, this was structurally incoherent — no single value is safe across a fallback chain spanning an 8k Ollama model and a 200k Claude model. Additionally, vision auto-switch is silently coupled to the fallback machinery: it only works when a vision-capable model happens to be in the fallback list, and hard-errors otherwise. Both couplings are worth breaking now: `fallback_models` is used 1-2 times ever, and the per-model context window is the actual goal.

## What Changes

- **Re-home vision routing onto an all-models scan.** When images are present and the active model isn't vision-capable, scan all configured `[[models]]` for one with `vision = true` (first by config order), route the image request there, and revert to the primary model afterward. Decouples vision from fallback.
- **Remove `fallback_models` entirely.** **BREAKING** for configs that set `agent.fallback_models`: the field is parsed but ignored with a deprecation warning. **BREAKING** for persisted scheduler jobs: existing `scheduler.toml` entries with per-job `fallback_models = [...]` are dropped/ignored on load with a deprecation warning. Deletes the per-job override trichotomy (None/[]/list), scheduler per-job persistence, tool arg/schema/descriptor, Telegram display, and the `LLMClient` fallback chain. The `LLMClient` becomes single-model.
- **Add per-model `context_window` config field.** New optional `context_window: int | None` on `ModelConfig`. At the compaction call site, `effective = model.context_window or agent.ctx_max_tokens`. Compaction threshold becomes `(effective - model.max_tokens) * 0.85` — reserves completion tokens, then applies margin. `agent.ctx_max_tokens` stays as the documented ceiling/default; zero migration for existing configs.

## Capabilities

### New Capabilities
- `per-model-context-window`: Per-model context window awareness for compaction, replacing the single static agent-level limit with an effective limit derived from the active model's configured window.

### Modified Capabilities
- `agent-runtime-construction`: The fallback trichotomy scenario is removed — `RuntimeOptions.fallback_models` and the None/[]/list inheritance semantics no longer exist.
- `native-tool-calling`: The "chat_with_tools_fallback chains fallback models" scenario is modified — there is no fallback chain; the LLM client is single-model.

## Impact

- **Code:** `config_schema.py` (new field + deprecation warning), `llm_client.py` (vision re-home + fallback removal), `context_manager.py` (threshold formula), `react_loop.py` (effective limit resolution), `agent_runtime.py`, `agent_controller.py`, `main.py`, `scheduler.py`, `sub_agent_supervisor.py`, `execution_plan.py`, `builtin_tools/{agents,schemas,descriptors,schedule}.py`, `telegram_formatter.py`, `vulture_whitelist.py` (add `ModelConfig.context_window`; remove stale fallback entries)
- **Tests:** `test_scheduler_fallback.py` deleted; `test_p2_vision_fallback.py` reworked for all-models scan + revert; `test_agent_runtime_{skeleton,characterization}.py` updated to drop fallback trichotomy; `execution_harness.py` pruned of `fallback_models` param; new tests for vision all-models scan + revert, per-model context window, and deprecation warning
- **Config:** `config.toml.example` updated with `context_window` field on `[[models]]`; `scheduler.toml.example` updated to remove per-job `fallback_models` field; deprecation note for `agent.fallback_models` and per-job `fallback_models`
- **Docs:** README updated for per-model context window; fallback_models references removed
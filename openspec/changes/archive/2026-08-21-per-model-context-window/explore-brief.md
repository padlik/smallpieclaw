# Explore Brief: per-model-context-window

## Alternatives rejected and why

1. **Dynamic context-window discovery via provider APIs** — rejected: OpenAI `/v1/models` does not expose context window; Ollama's key is architecture-specific (`llama.context_length`, `gpt.context_length`, etc.); Anthropic and Gemini expose it but inconsistent APIs mean 4 code paths + caching + fallback-to-config. The config field is needed anyway as the OpenAI fallback, so discovery adds complexity for 2/4 providers while the other 2 still need manual config.

2. **Provider window tables (hardcoded model→window map)** — rejected: staleness (new models ship weekly), wrong for fine-tunes/custom Ollama models, version ambiguity (gpt-4 has had 8k/32k/128k), maintenance burden. A stale table gives false confidence.

3. **Keep-but-simplify fallback_models (agent-level list only, remove per-job override)** — rejected after user confirmed fallback_models is used 1-2 times ever and vision auto-switch never fired in practice (no fallback configured). Full removal is cleaner and loses no real functionality.

4. **Flat 0.85 margin on raw context_window** — rejected: conflates output-token reservation with estimation safety. For small windows (8k), `max_tokens=1024` completion budget + estimator drift can overshoot. Fixed by reserving completion tokens first, then applying margin.

## Final approach: three coupled changes in dependency order

### Step 1: Re-home vision routing onto all-models scan
- When images present and active model not vision-capable, scan ALL `self._models` for `vision=true` (not just fallback candidates)
- Pick first by config order (array index)
- **Revert to primary after the image request** (per-request capability supplement, not permanent model change)
- Decouples vision from fallback machinery

### Step 2: Remove fallback_models entirely
- Delete from 14 source modules + `test_scheduler_fallback.py`
- Config backward compat: **warn and ignore** (deprecation warning, startup continues)
- `AgentConfig.fallback_models` field stays in parser but logs warning, value unused
- Removes: trichotomy (None/[]/list), per-job override, scheduler persistence, tool arg/schema/descriptor, telegram display
- `LLMClient._fallback_indices` removed; candidates loop becomes single-model

### Step 3: Per-model context_window config field
- Add `context_window: int | None` to `ModelConfig` (config_schema.py)
- At `react_loop.py:1164` call site: `effective = model.context_window or agent.ctx_max_tokens`
- Compaction threshold: `(effective - model.max_tokens) * 0.85` — reserves completion tokens, then applies margin
- `agent.ctx_max_tokens` stays as documented ceiling/default; zero migration

## Cross-module data flows

### Vision routing (after re-home)
- `llm_client.py:_run_with_fallback()` → checks `_messages_have_images()` → scans `self._models` for `vision=true` → sets `_active_idx` to vision model for this call → restores `primary_idx` after call returns (success or error)

### Context window resolution
- `config_schema.py:ModelConfig.context_window` (new field) → `main.py` reads config → `LLMClient` holds active model config → `react_loop.py:1164` reads `ctx.llm.llm_cfg["context_window"]` → computes `effective = cw or ctx.ctx_max_tokens` → passes `effective` to `maybe_compact()` → `context_manager.py` computes `threshold = int((effective - model.max_tokens) * 0.85)`

### Fallback removal
- `LLMClient.__init__` drops `fallback_models` param and `_fallback_indices` resolution
- `_run_with_fallback()` candidates loop simplified to single-model (no fallback chain)
- `RuntimeOptions.fallback_models` removed; `agent_runtime.py` no longer threads it
- `scheduler.py` drops `fallback_models` from `add_job`/meta/toml persistence
- `builtin_tools/agents.py`/`schemas.py`/`descriptors.py` drop the tool arg
- `telegram_formatter.py` drops job display

## Affected existing specs (need delta specs)

1. **agent-runtime-construction** — scenario "Model override and fallback trichotomy are preserved" (lines 26-31) must be REMOVED; fallback trichotomy no longer exists
2. **native-tool-calling** — scenario "chat_with_tools_fallback chains fallback models" (lines 116-120) must be MODIFIED; no fallback chain to try

## Open questions (resolved during grill-me)

1. ✅ Compaction margin: `(context_window - model.max_tokens) * 0.85` (reserve completion, flat margin)
2. ✅ Vision model selection: first by config order (array index)
3. ✅ Vision switch persistence: revert to primary after image request
4. ✅ fallback_models config compat: warn and ignore (deprecation warning)
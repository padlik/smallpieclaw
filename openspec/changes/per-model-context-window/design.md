## Context

The agent's context-size limit (`ctx_max_tokens`, default 90,000) is a single static agent-level value, decoupled from the actual model's context window. `fallback_models` (a per-job overrideable backup model chain) is used 1-2 times in the agent's entire history. Vision auto-switch is silently coupled to the fallback machinery — it only works when a vision-capable model happens to be in the fallback list, and hard-errors otherwise.

Three coupled changes break both couplings and add per-model context awareness:
1. Re-home vision routing onto an all-models scan (decouples vision from fallback)
2. Remove `fallback_models` entirely (removes the mid-run window-swing hazard)
3. Add per-model `context_window` config field (the actual goal)

ADR-0007 (agent runtime construction) is in-force and established `RuntimeOptions.fallback_models`. Removing that field requires a superseding ADR — flagged in Open Questions for the adr step.

## Goals / Non-Goals

**Goals:**
- Per-model context window awareness for compaction, replacing the single static limit
- Decouple vision routing from fallback machinery so vision works with any configured vision model
- Remove `fallback_models` and its trichotomy plumbing across 14 modules
- Backward-compatible config: existing `agent.fallback_models` and per-job `scheduler.toml` entries warn-and-ignore

**Non-Goals:**
- Dynamic context-window discovery via provider APIs (OpenAI doesn't expose it; Ollama's key is architecture-specific; config field needed anyway as fallback)
- Provider window tables (staleness, wrong for fine-tunes/custom models)
- Making drastic downward model transitions lossless (moot — fallback is being removed)
- Changing `chat_with_fallback` / `chat_with_tools_fallback` method names (kept as single-model no-op-fallback so existing native-tool-calling scenarios stay valid)

## Decisions

### D1: Vision routing scans all configured models, reverts to primary after request

**Decision:** When images are present and the active model isn't vision-capable, scan `self._models` in array order for the first with `vision = true`. Route the image request there. Restore `_active_idx` to `primary_idx` after the call returns (success or error).

**Why over alternatives:**
- *Persist on vision model (current fallback behavior):* rejected — vision is a per-request capability supplement, not a permanent model change. Persisting would run a more expensive vision model for all subsequent text turns.
- *Prefer same-provider vision model:* rejected — adds logic for marginal benefit; operator controls via `[[models]]` ordering.
- *Pick cheapest vision model:* rejected — no cost data in `ModelConfig`; `max_tokens` isn't a cost signal.

**Data flow:**
```
LLMClient._run_with_fallback()
  → _messages_have_images() == True
  → active model not vision-capable
  → scan self._models for vision=true (first by index)
  → set _active_idx = vision_idx for this call
  → call_fn()
  → restore _active_idx = primary_idx (finally block)
```

### D2: Remove fallback_models entirely; warn-and-ignore on config load

**Decision:** Delete `fallback_models` from 14 source modules + `test_scheduler_fallback.py`. `AgentConfig.fallback_models` field stays in the parser but logs a deprecation warning and the value is unused. Persisted per-job `fallback_models` in `scheduler.toml` is dropped/ignored on load with a deprecation warning. `LLMClient` becomes single-model; `_fallback_indices` removed; candidates loop simplified to `[primary_idx]`.

**Why over alternatives:**
- *Keep-but-simplify (agent-level list only, remove per-job override):* rejected after user confirmed fallback is used 1-2 times ever and vision auto-switch never fired in practice. Full removal is cleaner, loses no real functionality, and eliminates the mid-run window-swing hazard entirely.
- *Hard error on stale config:* rejected — too disruptive for a rarely-used feature being removed.
- *Silent ignore:* rejected — stale config looks valid; deprecation warning is the standard pattern.

**Affected modules:**
- `llm_client.py`: drop `fallback_models` param, `_fallback_indices`, candidates loop
- `agent_runtime.py`: drop `RuntimeOptions.fallback_models` and trichotomy
- `agent_controller.py`, `execution_plan.py`, `main.py`: drop factory kwargs threading
- `scheduler.py`: drop `fallback_models` from `add_job`/meta/toml persistence; warn-and-ignore on load
- `sub_agent_supervisor.py`: drop inherited-fallback logging
- `builtin_tools/{agents,schemas,descriptors,schedule}.py`: drop tool arg/schema/descriptor
- `telegram_formatter.py`: drop job display

### D3: Per-model context_window config field; compaction reserves completion tokens

**Decision:** Add `context_window: int | None` to `ModelConfig`. At `react_loop.py:1164`, compute `effective = model.context_window or agent.ctx_max_tokens`. Compaction threshold becomes `int((effective - model.max_tokens) * 0.85)` — reserves completion tokens first, then applies the 85% margin. `agent.ctx_max_tokens` stays as the documented ceiling/default.

**Why over alternatives:**
- *Flat 0.85 of raw context_window:* rejected — conflates output-token reservation with estimation safety. For small windows (8k), `max_tokens=1024` + estimator drift can overshoot.
- *Window-size-scaled margin (0.90 large, 0.80 small):* rejected — adds a scaling curve hard to tune without real telemetry. The small-window estimator drift is a pre-existing problem better addressed in `token_estimator.py`.
- *Dynamic discovery:* rejected — OpenAI `/v1/models` doesn't expose context window; Ollama's key is architecture-specific; inconsistent APIs mean 4 code paths + caching + config fallback anyway.

**Data flow:**
```
config_schema.py:ModelConfig.context_window (new field)
  → main.py reads config
  → LLMClient holds active model config (llm_cfg accessor)
  → react_loop.py:1164 reads ctx.llm.llm_cfg["context_window"]
  → effective = cw or ctx.ctx_max_tokens
  → passes effective to maybe_compact()
  → context_manager.py: threshold = int((effective - model.max_tokens) * 0.85)
```

### C4 Component Diagram (Mermaid)

```mermaid
flowchart LR
  subgraph Config
    CT[config.toml]
    ST[scheduler.toml]
  end

  subgraph AgentProcess
    CS[config_schema.py<br/>ModelConfig.context_window<br/>AgentConfig.fallback_models warn]
    LC[llm_client.py<br/>single-model + vision scan]
    CM[context_manager.py<br/>threshold = (eff - max_tokens) * 0.85]
    RL[react_loop.py<br/>effective = cw or ctx_max]
    AR[agent_runtime.py<br/>RuntimeOptions - fallback]
    SCH[scheduler.py<br/>drop fallback persistence]
  end

  CT --> CS
  ST --> SCH
  CS --> LC
  CS --> AR
  LC --> RL
  RL --> CM
  AR --> RL
  SCH --> LC
```

**Boundaries & responsibilities:**
- `config_schema.py` owns the new `context_window` field and the `fallback_models` deprecation warning
- `llm_client.py` owns vision all-models scan + revert, and single-model operation
- `context_manager.py` owns the new threshold formula (completion-token reservation)
- `react_loop.py` owns effective-limit resolution at the compaction call site
- `agent_runtime.py` drops the fallback trichotomy from `RuntimeOptions`
- `scheduler.py` drops fallback persistence and warn-ignores on load

## Risks / Trade-offs

- **[Persisted scheduler jobs with fallback_models fail on load]** → Mitigation: warn-and-ignore on load; drop the key from `spawn_args` so the factory doesn't receive it. Document in `scheduler.toml.example`.
- **[Vision model not configured but images sent]** → Mitigation: preserve the existing `LLMPermanentError` with the same message ("no vision-capable model is configured"). Behavior unchanged for users without a vision model.
- **[Token estimator drift at small windows]** → Mitigation: the new formula `(effective - max_tokens) * 0.85` reserves completion tokens, which is the main fix. Remaining estimator drift is pre-existing and out of scope for this change.
- **[ADR-0007 supersession]** → Mitigation: flag in Open Questions; adr step records the superseding ADR. `RuntimeOptions.fallback_models` removal is a decision-level change to an in-force ADR.
- **[chat_with_fallback / chat_with_tools_fallback method names]** → Mitigation: keep method names as single-model no-op-fallback so existing native-tool-calling scenarios stay valid. Only the "chains fallback models" scenario is modified.

## Migration Plan

1. **Re-home vision routing** (independent change, unblocks step 2)
2. **Remove fallback_models** (unblocks step 3)
3. **Add per-model context_window** (now trivial — one model per client)

**Rollback:** Each step is independently revertible via git. Step 1 (vision) and step 3 (context_window) are additive. Step 2 (fallback removal) is the breaking change; rollback restores the deleted code and config field.

**Config migration:** None required. Existing configs without `context_window` use `agent.ctx_max_tokens` as before. Existing configs with `agent.fallback_models` or per-job `fallback_models` get a deprecation warning and continue working (fallback ignored).

## Open Questions

- **ADR-0007 supersession:** ADR-0007 (in-force) established `RuntimeOptions.fallback_models`. Removing that field is a decision-level change. The adr step should record a superseding ADR documenting the removal and the rationale (rarely used, vision decoupled, simplifies per-model context window work).
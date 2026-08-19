# Remove fallback_models and add per-model context window

## Status

Accepted, partially supersedes ADR-0007

## Date

2026-08-19

## Supersedes

ADR-0007 (partially: only the `RuntimeOptions.fallback_models` trichotomy is removed; the AgentRuntime construction boundary, `RuntimeProfile`, and other `RuntimeOptions` knobs remain in force)

## Context

ADR-0007 established `AgentRuntime` as the construction boundary and `RuntimeOptions` as the per-execution knob carrier, including `fallback_models` with a None/[]/list trichotomy (inherit/disable/use). Two problems emerged:

1. `fallback_models` is used 1-2 times in the agent's entire history. The trichotomy propagates through 14 modules (sub-agent, scheduled, plan-step paths) with per-job persistence in `scheduler.toml` — heavy plumbing for a rarely-used feature.

2. Vision auto-switch is silently coupled to the fallback machinery: it only works when a vision-capable model happens to be in the fallback list, and hard-errors otherwise. The user confirmed vision auto-switch never fired in practice because no fallback was configured.

3. The single static `ctx_max_tokens` (default 90k) is decoupled from the actual model's context window. With `fallback_models`, this was structurally incoherent — no single value is safe across a fallback chain spanning an 8k Ollama model and a 200k Claude model.

## Considered Options

- **Keep-but-simplify:** Collapse to a single agent-level `fallback_models` list, remove per-job override. Rejected after user confirmed fallback is used 1-2 times ever and vision auto-switch never fired. Full removal is cleaner, loses no real functionality, and eliminates the mid-run window-swing hazard.
- **Full removal (chosen):** Delete `fallback_models` entirely, re-home vision routing onto an all-models scan, add per-model `context_window` config field. Warn-and-ignore on stale config.
- **Dynamic context-window discovery:** Rejected — OpenAI `/v1/models` doesn't expose context window; Ollama's key is architecture-specific; inconsistent APIs mean 4 code paths + caching + config fallback anyway.

## Decision

Remove `fallback_models` from `RuntimeOptions` and the entire fallback chain from `LLMClient`. The LLM client becomes single-model. Vision routing is re-homed onto an all-models scan (first vision-capable model by config order, revert to primary after the request). Add `context_window: int | None` to `ModelConfig`; compaction uses `effective = model.context_window or agent.ctx_max_tokens` with threshold `int((effective - model.max_tokens) * 0.85)`. Existing `agent.fallback_models` and per-job `scheduler.toml` entries are parsed but ignored with a deprecation warning.

## Consequences

- Good, because per-model context window awareness is now trivial — one model per client, no mid-run window transitions.
- Good, because vision routing works with any configured vision model, not just those in a fallback list.
- Good, because 14 modules of trichotomy plumbing and a dedicated test file are deleted.
- Bad, because provider-outage resilience is lost — a single provider failure now propagates to the caller with no automatic retry on a backup model. Accepted because the feature was used 1-2 times ever and the agent is single-user by nature.
- Bad, because existing configs with `agent.fallback_models` or per-job `fallback_models` require cleanup (deprecation warning guides this).
- Follow-up: `vulture_whitelist.py` must be updated (new `ModelConfig.context_window`, removed `RuntimeOptions.fallback_models` / `_fallback_indices`).
# per-model-context-window Specification

## Purpose

Define how the agent compaction and routing layers use per-model context-window configuration, how vision-bearing requests are routed across all configured models, and how the deprecated fallback-models feature is removed from the single-model LLM client.

## Requirements

### Requirement: Per-model context window awareness

The compaction system SHALL derive the effective context-window limit from the active model's configured `context_window` field, falling back to `agent.ctx_max_tokens` when unset, and SHALL reserve completion tokens before applying the compaction margin. The compaction threshold SHALL account for tool-definition token cost in addition to system prompt and chat history, so the 85% margin is computed against the real payload size sent to the LLM.

#### Scenario: Model with context_window set uses per-model limit
- **GIVEN** the active model's `ModelConfig.context_window` is set to a non-null value
- **WHEN** the ReAct loop calls `maybe_compact()`
- **THEN** the effective limit SHALL be the model's `context_window`
- **AND** the compaction threshold SHALL be `max(int((effective - model.max_tokens) * 0.85), 256)`

#### Scenario: Model without context_window falls back to agent default
- **GIVEN** the active model's `ModelConfig.context_window` is null or absent
- **WHEN** the ReAct loop calls `maybe_compact()`
- **THEN** the effective limit SHALL be `agent.ctx_max_tokens`
- **AND** the compaction threshold SHALL be `max(int((effective - model.max_tokens) * 0.85), 256)`

#### Scenario: Completion tokens are reserved before margin
- **GIVEN** a model with `context_window = 8192` and `max_tokens = 1024`
- **WHEN** the compaction threshold is computed
- **THEN** the threshold SHALL be `max(int((8192 - 1024) * 0.85), 256)` = 6092
- **AND** the threshold SHALL NOT be `int(8192 * 0.85)` = 6963

#### Scenario: Effective limit is resolved per-turn at the compaction call site
- **GIVEN** the ReAct loop is running with an active model
- **WHEN** `maybe_compact()` is called on each turn
- **THEN** the effective limit SHALL be read from the active model config via `ctx.llm.llm_cfg`
- **AND** no mid-run model transition SHALL occur (single-model client)

#### Scenario: Tool-definition tokens included in compaction total
- **GIVEN** the ReAct loop has built tool definitions via `build_tool_definitions()`
- **AND** the tool definitions consume 18,000 tokens
- **WHEN** `maybe_compact()` is called
- **THEN** the total used for threshold comparison SHALL include the tool-definition token cost
- **AND** the total SHALL be `estimate_messages_tokens(messages, system) + tool_defs_tokens`
- **AND** if the total exceeds the threshold, compaction SHALL trigger

#### Scenario: Tool-definition tokens default to zero for backward compatibility
- **GIVEN** a caller invokes `maybe_compact()` without passing `tool_defs_tokens`
- **WHEN** the compaction total is computed
- **THEN** the tool-definition token cost SHALL be 0
- **AND** the behavior SHALL be identical to the pre-change compaction logic

### Requirement: Vision routing via all-models scan

The LLM client SHALL route image-bearing requests to a vision-capable model by scanning all configured `[[models]]` entries, independent of any fallback list, and SHALL revert to the primary model after the request completes.

#### Scenario: Image request routes to first vision-capable model by config order
- **GIVEN** the active model is not vision-capable (`vision = false`)
- **AND** at least one other `[[models]]` entry has `vision = true`
- **WHEN** a request containing images is sent
- **THEN** the LLM client SHALL scan `self._models` in array order
- **AND** SHALL route the request to the first model with `vision = true`
- **AND** SHALL emit a progress message indicating the switch

#### Scenario: Vision switch reverts to primary after request
- **GIVEN** the LLM client routed an image request to a vision-capable model
- **WHEN** the request completes (success or error)
- **THEN** the active model index SHALL be restored to the primary model
- **AND** the next text-only request SHALL use the primary model

#### Scenario: No vision-capable model configured
- **GIVEN** the active model is not vision-capable
- **AND** no `[[models]]` entry has `vision = true`
- **WHEN** a request containing images is sent
- **THEN** the LLM client SHALL raise `LLMPermanentError`
- **AND** the error message SHALL indicate that no vision-capable model is configured

#### Scenario: Active model is already vision-capable
- **GIVEN** the active model has `vision = true`
- **WHEN** a request containing images is sent
- **THEN** no model switch SHALL occur
- **AND** the request SHALL be sent to the active model directly

### Requirement: Fallback models deprecation

The system SHALL parse `agent.fallback_models` and per-job `fallback_models` for backward compatibility but SHALL ignore the values with a deprecation warning, and the LLM client SHALL operate as a single-model client without a fallback chain.

#### Scenario: Agent-level fallback_models warns and ignores
- **GIVEN** a config with `agent.fallback_models = ["model-b"]`
- **WHEN** the config is parsed at startup
- **THEN** the parser SHALL log a deprecation warning
- **AND** the value SHALL NOT be used for model selection or fallback

#### Scenario: Persisted per-job fallback_models warns and ignores on load
- **GIVEN** an existing `scheduler.toml` with a job containing `fallback_models = ["model-b"]`
- **WHEN** the scheduler loads jobs at startup
- **THEN** the scheduler SHALL log a deprecation warning
- **AND** SHALL drop the `fallback_models` key from the job meta
- **AND** SHALL NOT pass it to the sub-agent spawn factory

#### Scenario: LLM client operates single-model
- **GIVEN** an `LLMClient` instance is constructed
- **WHEN** a chat request fails with a transient error
- **THEN** the client SHALL NOT try any fallback model
- **AND** SHALL propagate the error to the caller

## MODIFIED Requirements

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
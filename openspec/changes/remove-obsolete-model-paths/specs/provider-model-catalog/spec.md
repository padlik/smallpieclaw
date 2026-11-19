## MODIFIED Requirements

### Requirement: Obsolete agent model and context keys are rejected

Configuration validation SHALL reject `[agent]` `default_model`, `background_model`, `fallback_model`, `fallback_models`, `ctx_max_tokens`, and `diagnose_empty_responses` with an actionable error naming the key and its replacement or removal, and SHALL NOT ignore or reinterpret them. Rejection of `diagnose_empty_responses` SHALL use the existing removed-field validation with fixed guidance to remove the key because empty-response diagnostics were removed, and SHALL NOT change how other unknown keys are handled.

#### Scenario: Obsolete agent selector is rejected
- **GIVEN** configuration contains `agent.default_model`, `agent.background_model`, `agent.fallback_model`, or `agent.fallback_models`
- **WHEN** configuration is validated
- **THEN** it reports an actionable error pointing to `/models` and does not infer a selection

#### Scenario: Obsolete context limit is rejected
- **GIVEN** configuration contains `agent.ctx_max_tokens`
- **WHEN** configuration is validated
- **THEN** it reports an actionable error directing the operator to `agent.compaction_threshold_tokens`

#### Scenario: Retired empty-response diagnostics key is rejected
- **GIVEN** configuration contains `agent.diagnose_empty_responses` with any value
- **WHEN** configuration is validated
- **THEN** startup fails with an error naming the key and telling the operator to remove it because diagnostics were removed
- **AND** the key is not ignored, no diagnostic runs, and context monitoring and empty-response classification and retries are unaffected

## REMOVED Requirements

### Requirement: The retired combined model settings file receives only an existence notice

**Reason**: The runtime no longer references `models-config.toml` or its path; code for the existence check and notice is deleted.

**Migration**: None. Any existing `models-config.toml` is ignored and left unread and untouched on disk; operators may delete it manually and choose a model with `/models`.

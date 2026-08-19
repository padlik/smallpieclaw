## MODIFIED Requirements

### Requirement: Runtime options preserve construction knobs
The runtime SHALL accept per-execution options for construction values currently duplicated across agent construction call sites.

#### Scenario: Model override is preserved
- **GIVEN** a caller provides a model override
- **WHEN** the runtime constructs a sub-agent product
- **THEN** the constructed execution SHALL use the specified model as the active model
- **AND** token usage accounting continues to use the configured usage registry
- **AND** caller tagging remains equivalent to the legacy construction path

#### Scenario: Per-call LLM overrides are preserved
- **GIVEN** a caller provides `max_tokens`, `temperature`, or `top_p` overrides
- **WHEN** the runtime constructs a sub-agent product
- **THEN** the constructed execution uses those overrides without mutating shared model configuration
- **AND** token usage accounting continues to use the configured usage registry
- **AND** caller tagging remains equivalent to the legacy construction path

#### Scenario: Context and prompt options are preserved
- **GIVEN** a caller provides `context_key`, `context_payload`, `prompt_variant`, `trace_id`, `cancel_event`, `label`, and `max_iterations`
- **WHEN** the runtime constructs an execution product
- **THEN** those values are applied to the product or generated context according to the selected profile
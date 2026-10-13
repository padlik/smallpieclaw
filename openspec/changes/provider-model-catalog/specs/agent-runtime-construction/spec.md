# agent-runtime-construction Specification

## Purpose

Modify runtime model selection and remove per-call generation overrides without changing profiles, products, context assembly, supervision, or scheduling.

## REMOVED Requirements

### Requirement: Runtime options preserve construction knobs

**Reason:** The prior construction-knob contract permitted obsolete per-call generation overrides.

**Migration:** `migrate_models.py` omits obsolete `max_tokens`, `temperature`, and `top_p` overrides. Supported runtime options and model selection remain available.

## ADDED Requirements

### Requirement: Runtime options preserve supported construction values

Runtime options SHALL accept an admission-resolved model snapshot and existing supported context/prompt/run options. They SHALL reject per-call `max_tokens`, `temperature`, and `top_p` before model admission or provider I/O.

#### Scenario: Model override is preserved
- **GIVEN** catalog admission resolves an explicit canonical model override
- **WHEN** runtime constructs a sub-agent product
- **THEN** it uses the immutable admitted model/connection/metadata snapshot for its full run
- **AND** token usage accounting and caller tagging remain equivalent to the legacy path

#### Scenario: Per-call generation overrides are rejected
- **GIVEN** a caller supplies `max_tokens`, `temperature`, or `top_p`
- **WHEN** builtin argument validation runs
- **THEN** it returns an actionable obsolete-argument error before admission
- **AND** it neither ignores nor forwards the override

#### Scenario: Context and prompt options are preserved
- **GIVEN** a caller provides `context_key`, `context_payload`, `prompt_variant`, `trace_id`, `cancel_event`, `label`, and `max_iterations`
- **WHEN** the runtime constructs an execution product
- **THEN** the supported values are applied according to the existing profile
- **AND** runtime/profile, supervision, callback, and context ownership remain unchanged

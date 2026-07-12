## ADDED Requirements

### Requirement: Context payload and supervision channel separation
Model-facing sub-agent context payloads SHALL remain separate from internal supervision controls used for scheduling, notification, result logging, and cleanup.

#### Scenario: Model-provided context remains model-facing
- **GIVEN** the model calls `spawn_agent` with a valid `context_payload`
- **WHEN** the sub-agent is spawned
- **THEN** the payload is treated as transient parent context for the sub-agent prompt
- **AND** scheduler/internal supervision controls are not inferred from that payload

#### Scenario: Internal supervision controls are not context payload
- **GIVEN** a scheduled job launches a sub-agent with job metadata and callbacks
- **WHEN** the sub-agent receives parent context
- **THEN** scheduler/internal control values are not injected into the sub-agent's `PARENT CONTEXT` section
- **AND** the existing context payload size, auto-summary, prompt injection, and persistence-exclusion rules remain unchanged

## MODIFIED Requirements

## REMOVED Requirements

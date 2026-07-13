## ADDED Requirements

### Requirement: Supervised agents use runtime construction
Supervised on-demand and scheduled sub-agent runs SHALL use the shared runtime construction boundary while preserving existing supervision behavior.

#### Scenario: On-demand supervised construction uses runtime
- **GIVEN** the model-facing `spawn_agent` path admits an on-demand run
- **WHEN** the sub-agent product is constructed
- **THEN** construction flows through the runtime profile for on-demand sub-agents
- **AND** the supervision lifecycle, registry source, capacity behavior, result retrieval, context persistence, timeout cancellation, and graph-memory non-admission behavior remain compatible

#### Scenario: Scheduled supervised construction uses runtime
- **GIVEN** the scheduler admits a scheduled sub-agent run
- **WHEN** the sub-agent product is constructed
- **THEN** construction flows through the runtime profile for scheduled agents
- **AND** scheduler callbacks, result logging, notification options, registry source, and capacity behavior remain compatible

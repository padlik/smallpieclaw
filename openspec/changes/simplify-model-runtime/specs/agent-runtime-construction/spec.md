# agent-runtime-construction Delta

## Purpose

Baseline: canonical `openspec/specs/agent-runtime-construction/spec.md` as already changed by the pending predecessor delta `openspec/changes/provider-model-catalog/specs/agent-runtime-construction/spec.md` (implementation baseline `a57ab26`), which removes `Runtime options preserve construction knobs` and adds `Runtime options preserve supported construction values`. This delta MUST be synchronized only after that predecessor delta. It changes only where the immutable run target comes from; `Profile-based runtime construction`, `Runtime-built products preserve consumer surface`, and `Runtime context equivalence` are unchanged.

## MODIFIED Requirements

### Requirement: Runtime options preserve supported construction values

Runtime options SHALL accept the immutable run target produced by direct admission — an explicit selector resolved against the fixed connection map, or a snapshot of the active main selection when no selector is given — together with existing supported context, prompt, and run options. Construction SHALL NOT require an active main selection or catalog membership for an explicit target, and SHALL NOT re-admit, re-resolve credentials, or consult the catalog cache. Runtime options SHALL reject per-call `max_tokens`, `temperature`, and `top_p` before model admission or provider I/O.

#### Scenario: Model override is preserved
- **GIVEN** direct admission resolves an explicit selector for a configured connection, even when the model is not cached and no main selection exists
- **WHEN** runtime constructs a sub-agent product
- **THEN** it uses that immutable admitted target for its full run
- **AND** token usage accounting and caller tagging remain equivalent to the legacy path

#### Scenario: Inherited target is snapshotted once
- **GIVEN** a sub-agent is requested without a selector while a main selection is active
- **WHEN** the main selection changes after admission
- **THEN** the constructed product keeps the target snapshotted at admission

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

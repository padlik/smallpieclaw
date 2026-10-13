# Pin admission-resolved models at runtime construction

## Status

accepted, supersedes ADR-0007 (partial: per-call generation overrides and model-resolution assumptions only)

## Date

2026-10-13

## Supersedes

ADR-0007 (partial: replace model resolution and per-call generation-tuning assumptions with admission-resolved immutable run snapshots and rejection of per-call `max_tokens`, `temperature`, and `top_p`. The AgentRuntime construction boundary, profiles, compatible products, and separation from supervision and orchestration remain in force.)

## Context

ADR-0007 established AgentRuntime as the construction boundary and RuntimeOptions as the carrier for per-execution construction values, including model overrides. With catalog-backed admission, resolving a model during construction would permit construction paths to disagree about active selection and would weaken the guarantee that one endpoint, model, and metadata set stays fixed for a run. Per-call generation overrides also conflict with the shared request-parameter policy. The composition root remains `main.py`; this decision does not create another resolver or executor.

## Decision

Model admission resolves an explicit canonical model ID when supplied; otherwise it snapshots the active main model. It passes an immutable `RunModel` containing the admitted model, provider connection, endpoint, and metadata through the existing main or AgentRuntime construction path. The snapshot is pinned for the full run; later selection or catalog refresh does not change it. Per-call `max_tokens`, `temperature`, and `top_p` are rejected before model admission or provider I/O. Existing supported runtime values and the AgentRuntime profile/product, supervision, callback, and context-ownership boundaries remain unchanged.

## Consequences

- Construction consumes a resolved run contract rather than independently selecting a model, so all provider calls in a run remain tied to one admission decision.
- Callers must select a different model at admission rather than tune generation parameters per invocation; rejected obsolete arguments receive an actionable error.
- The main application remains composed in `main.py`, and AgentRuntime remains a construction boundary rather than a second executor or orchestration layer.
- ADR-0007's other construction commitments remain in force; catalog metadata and budgeting policy are governed separately by ADR-0034.

# per-model-context-window Specification

## Purpose

Specify catalog-derived limits, actual payload budgeting, compaction, and pinned-run image behavior.

## MODIFIED Requirements

### Requirement: Per-model context window awareness

The system SHALL budget the actual provider payload, including system prompt, messages, tool definitions, native-call arguments/framing, and image estimates. Let `I` be estimated input; `C` documented combined capacity; `T` separate input capacity; `O` output ceiling; `R` reserve.

Hard input budget `B` SHALL be `C-R` when only `C` is known, `T` when only `T` is known, `min(C-R,T)` when both are known, or `C-R` when neither is known and `C=agent.ctx_max_tokens` is labelled assumed local capacity. The system SHALL NOT infer `C` from `T+O` absent documented semantics. Known `T` SHALL NOT be reduced by `O` or local context.

Ordinary requests SHALL omit an optional output cap. Reserve full positive `O` only against `C`; when `O` is unknown, 1024 is local accounting only. A required output cap needs known `O`: use `min(O,C-I)` when `C` exists, otherwise `O` for input-only `T`.

The system SHALL reject non-positive `B`, combined-only `O>=C`, unknown required `O`, or `I>B` before provider I/O. Compaction threshold SHALL be `max(int(B*0.85),256)` and SHALL NOT override hard fit. Tool definitions and native-call arguments SHALL count in estimation; ordinary requests omit temperature/top_p.

#### Scenario: Model with context_window set uses per-model limit
- **GIVEN** the pinned model has documented `C=20,000`, `T=18,000`, and known optional `O=4,000`
- **WHEN** the ReAct loop calls `maybe_compact()`
- **THEN** hard input budget is `min(20,000-4,000,18,000)=16,000`

#### Scenario: Model without context_window falls back to agent default
- **GIVEN** neither `C` nor `T` is documented and `agent.ctx_max_tokens=12,000`
- **WHEN** the ReAct loop calls `maybe_compact()`
- **THEN** 12,000 is labelled assumed local `C`, not provider capacity
- **AND** the applicable reserve is subtracted from that local `C`

#### Scenario: Completion tokens are reserved before margin
- **GIVEN** combined `C=8192`, optional positive `O=1024`, and a complete payload estimate
- **WHEN** the hard budget and compaction threshold are calculated
- **THEN** `B=7168` and threshold is `max(int(7168*0.85),256)`
- **AND** the threshold is not computed from `C` alone

#### Scenario: Effective limit is resolved per-turn at the compaction call site
- **GIVEN** a run has an immutable admitted model snapshot
- **WHEN** `maybe_compact()` is called on each turn
- **THEN** its `C/T/O` metadata comes from that pinned model
- **AND** no mid-run model transition occurs

#### Scenario: Tool-definition tokens included in compaction total
- **GIVEN** tool definitions consume 18,000 tokens
- **WHEN** compaction compares the request to its threshold
- **THEN** used input includes `estimate_messages_tokens(messages,system)+tool_defs_tokens`
- **AND** native-call arguments/framing are included in actual wire budgeting

#### Scenario: Tool-definition tokens default to zero for backward compatibility
- **GIVEN** a legacy caller omits `tool_defs_tokens`
- **WHEN** its compaction total is computed
- **THEN** the added tool-definition cost is zero
- **AND** the legacy call remains compatible

#### Scenario: Gemini input ceiling is not reduced by output or local context
- **GIVEN** Gemini has `T=1,000,000`, `O=65,536`, no `C`, and local `agent.ctx_max_tokens=90,000`
- **WHEN** the hard input budget is computed
- **THEN** `B=1,000,000`, not 24,464 or 90,000

#### Scenario: Required output cap is unknown
- **GIVEN** the provider requires an output cap but `O` is unknown
- **WHEN** the request is prepared
- **THEN** it fails actionably before provider I/O

#### Scenario: Unknown optional output uses local reserve but omits wire cap
- **GIVEN** combined `C=8192`, optional `O` is unknown, and the provider permits omission
- **WHEN** the request is prepared
- **THEN** no output cap is sent and local `R=1024` gives `B=7168`
- **AND** the reserve is labelled assumed accounting, not API capacity

#### Scenario: Required output cap is bounded by remaining combined capacity
- **GIVEN** required output cap, `C=10,000`, `I=7,000`, and `O=4,000`
- **WHEN** the provider request is prepared
- **THEN** the sent output cap is `min(4,000,10,000-7,000)=3,000`

#### Scenario: Required cap with no positive remaining combined capacity fails
- **GIVEN** required output cap and `C-I` is zero or negative
- **WHEN** the request is prepared
- **THEN** it fails locally before provider I/O

#### Scenario: Input-only capacity bounds required output independently
- **GIVEN** `T=1,000,000`, `O=65,536`, no `C`, and `agent.ctx_max_tokens=90,000`
- **WHEN** a required output cap is prepared
- **THEN** cap is `65,536` and hard input budget is `1,000,000`
- **AND** no assumed combined context is subtracted

#### Scenario: Known optional output leaves no combined input budget
- **GIVEN** combined-only `C` and optional known `O>=C`
- **WHEN** the request is budgeted
- **THEN** it is rejected locally without reducing the reserve

#### Scenario: Compaction floor does not override hard fit
- **GIVEN** positive hard input budget `B` is below 256 tokens
- **WHEN** the heuristic threshold is calculated
- **THEN** it remains `max(int(B*0.85),256)`
- **AND** a payload exceeding `B` is still rejected

## REMOVED Requirements

### Requirement: Vision routing via all-models scan

**Reason:** Image routing is replaced by explicit capability validation on the immutable run model; no model scan or request-time switch is allowed.

**Migration:** `migrate_models.py` removes legacy auto-routing configuration. Select a verified vision model via `/models` or start a new task with its explicit canonical model ID.

#### Scenario: Image request routes to first vision-capable model by config order
- **GIVEN** the pinned run model is not verified vision-capable
- **WHEN** an image request is made
- **THEN** it is rejected locally rather than routed to another model

#### Scenario: Vision switch reverts to primary after request
- **GIVEN** a run is pinned to a model
- **WHEN** its image request completes
- **THEN** the run remains pinned to that same model

#### Scenario: No vision-capable model configured
- **GIVEN** pinned vision is false or unknown
- **WHEN** an image request is made
- **THEN** an actionable local error is returned before provider I/O

#### Scenario: Active model is already vision-capable
- **GIVEN** the pinned model has verified `vision=true`
- **WHEN** an image request is made
- **THEN** it is sent through that pinned model

### Requirement: Fallback models deprecation

**Reason:** Agent fallback settings become actionable configuration errors; scheduled selectors fail after execution-record creation, with no model fallback or job converter.

**Migration:** `migrate_models.py` removes obsolete agent fallback/background/default selectors. Obsolete job selectors require manual operator update; migration does not rewrite jobs and no job converter is provided.

#### Scenario: Agent-level fallback_models warns and ignores
- **GIVEN** configuration contains obsolete agent fallback settings
- **WHEN** configuration is parsed
- **THEN** it fails actionably instead of accepting a hidden fallback

#### Scenario: Persisted per-job fallback_models warns and ignores on load
- **GIVEN** a persisted job names an obsolete model/fallback selector
- **WHEN** the scheduled execution record is minted and admission is attempted
- **THEN** that launch is recorded as failed and the existing alert is sent
- **AND** no job converter or model fallback runs

#### Scenario: LLM client operates single-model
- **GIVEN** an `LLMClient` is pinned to one run model
- **WHEN** a transient provider error occurs
- **THEN** no other catalog model is tried

## ADDED Requirements

### Requirement: Vision requests use the pinned run model

All image requests SHALL use the admitted run model. Only verified `vision=true` permits images; false/unknown fails locally with `/models` or new-task guidance. `vision_query` SHALL have no model override.

#### Scenario: Pinned model lacks verified vision support
- **GIVEN** direct image or `vision_query` is requested and pinned vision is false or unknown
- **WHEN** the request is prepared
- **THEN** it fails actionably before provider I/O without scanning models or switching endpoints

#### Scenario: Verified vision capability uses pinned model
- **GIVEN** pinned model metadata says `vision=true`
- **WHEN** direct image or `vision_query` is sent
- **THEN** that pinned model receives it and image estimates count toward the input budget

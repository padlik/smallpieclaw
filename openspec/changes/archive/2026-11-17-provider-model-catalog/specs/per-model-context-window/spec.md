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

**Reason:** Agent fallback settings become actionable configuration errors. A legacy per-job `fallback_models` setting is no longer dropped at load; it is ignored with a runtime warning when the job executes, never as a load or startup error, with no model fallback or job converter.

**Migration:** `migrate_models.py` removes obsolete agent fallback/background/default selectors. It does not rewrite jobs and no job converter is provided; an operator may remove a legacy per-job `fallback_models` manually, and until then it is ignored with a warning at execution (see `scheduled-job-execution`).

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

## ADDED Requirements

### Requirement: Obsolete fallback settings never create model fallback

Explicit agent-level fallback settings in human configuration SHALL be rejected with an actionable configuration error. A legacy per-job `fallback_models` setting SHALL be ignored with a runtime warning when the job executes, never as a load or startup error. Neither SHALL enable model fallback, and every LLM client SHALL remain single-model for its run.

#### Scenario: Agent-level fallback setting is rejected
- **GIVEN** human configuration contains an agent-level fallback setting
- **WHEN** configuration is validated
- **THEN** validation fails with an actionable error and the value is not used for selection

#### Scenario: Legacy per-job fallback is ignored at execution
- **GIVEN** a scheduled job carries `fallback_models`
- **WHEN** the job executes
- **THEN** the setting is ignored with a runtime warning and the job configuration is not rewritten
- **AND** no cross-model retry is enabled

#### Scenario: Pinned client is single-model
- **GIVEN** an `LLMClient` is pinned to one run model
- **WHEN** a transient provider error occurs
- **THEN** no other catalog model is tried

### Requirement: Request budget limits are validated strictly

Each metadata limit used by request budgeting SHALL be absent or an exact positive integer. A boolean, string, zero, or negative limit SHALL be rejected as an invalid limit before provider I/O, and SHALL NOT be treated as unknown.

#### Scenario: Malformed limit is rejected
- **GIVEN** a limit is `True`, `"4096"`, `0`, or `-1`
- **WHEN** the request is budgeted
- **THEN** it fails with an invalid-limit error before provider I/O

#### Scenario: Absent limit is unknown
- **GIVEN** a limit is `None`
- **WHEN** the request is budgeted
- **THEN** it is treated as unknown under the documented budget cases

### Requirement: Fixed-output graph budgeting preserves its named exception

Graph extraction SHALL use fixed `temperature=0.1` and `max_tokens=1024`, which is also its completion reserve. An unknown output ceiling SHALL be allowed, a known `O` below 1024 SHALL fail, and an assumed local budget SHALL apply only when neither `C` nor `T` is known. The hard input budget SHALL be the minimum of the known candidates `C-1024` and `T`; non-positive headroom SHALL fail.

#### Scenario: Unknown output ceiling is allowed
- **GIVEN** graph extraction with unknown `O` and known `C=8192`
- **WHEN** the request is budgeted
- **THEN** the fixed 1024 cap is sent and the hard input budget is `7168`

#### Scenario: Known output below the fixed cap fails
- **GIVEN** graph extraction with known `O=512`
- **WHEN** the request is budgeted
- **THEN** it fails with an output-limit-too-small error and no model is substituted

#### Scenario: Local budget applies only when no capacity is known
- **GIVEN** neither `C` nor `T` is known and `agent.ctx_max_tokens=12,000`
- **WHEN** the graph request is budgeted
- **THEN** 12,000 is a labelled assumed local `C`
- **AND** with `T` known, the local value is not used

#### Scenario: Hard input takes the minimum known candidate
- **GIVEN** `C=10,000` and `T=8,000`
- **WHEN** the graph request is budgeted
- **THEN** the hard input budget is `min(10,000-1,024,8,000)=8,000`

#### Scenario: Non-positive headroom fails
- **GIVEN** the fixed cap leaves no positive input budget
- **WHEN** the request is budgeted
- **THEN** it fails with a no-input-budget error before provider I/O

#### Scenario: Prerequisite and actual checks run before inference
- **GIVEN** graph startup and each extraction
- **WHEN** the budget is checked
- **THEN** startup checks with zero input and each extraction rechecks its actual input before inference and usage accounting

#### Scenario: Ordinary unknown required output still fails
- **GIVEN** an ordinary request requires an output cap and `O` is unknown
- **WHEN** it is prepared
- **THEN** it fails actionably, because the graph exception is not extended to ordinary requests

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

### Requirement: A manual smaller-model switch keeps selection and guards oversized requests

A manual smaller-model switch SHALL install a conversation guard on the full R6 identity: canonical model ID, selection revision, generation, token. Guarded requests are estimated read-only; oversized rejections keep the guard until reset or a larger switch; a fitting candidate clears it. `Larger` compares the new and preceding selections' effective hard input budgets `B` in the existing `C`/`T`/`O` cases, labelled assumed local `C` included.

#### Scenario: Oversized first request after a smaller switch is rejected
- **GIVEN** the operator manually switches the conversation to a smaller model and the assembled candidate exceeds the new selection's budget cases
- **WHEN** the first request under that selection is prepared
- **THEN** it is rejected before provider I/O without truncating history or source
- **AND** the selected model, history, and conversation source are preserved

#### Scenario: Fitting request proceeds and clears the guard
- **GIVEN** a manual switch whose first candidate fits the new selection's budget cases
- **WHEN** the request is sent and succeeds
- **THEN** the guard is cleared and ordinary compaction remains available for other requests

#### Scenario: Guard persists until reset or a larger switch
- **GIVEN** a guard installed by a smaller-model switch
- **AND** oversized candidates were rejected without truncation and no fitting candidate has since cleared the guard
- **WHEN** further requests run before a reset or a manual change to a larger model
- **THEN** each candidate is still estimated read-only and rechecked against the budget before transmission
- **AND** the existing compactor is not rewritten and the selection remains unchanged

#### Scenario: Restored history under a different selection is guarded without migration
- **GIVEN** conversation history is present under a different selection revision than the current conversation's guard revision
- **WHEN** the next request is prepared
- **THEN** the guard evaluates the complete candidate read-only against the new selection's budget cases
- **AND** no history migration or silent truncation occurs

#### Scenario: Ordinary compaction remains available with the guard installed
- **GIVEN** a guard installed after a smaller-model switch
- **WHEN** later requests run under that selection
- **THEN** ordinary compaction remains available for them
- **AND** the existing compactor is not blanket-disabled or rewritten
- **AND** compaction runs only after the guarded candidate is evaluated read-only and fits, or for unguarded requests
- **AND** an oversized guarded candidate is rejected and is never compacted to force a fit

#### Scenario: Fitting first candidate is estimated read-only before history changes
- **GIVEN** a manual smaller-model switch and a first candidate that fits its budget cases
- **WHEN** the first request under that selection is prepared
- **THEN** the complete candidate is assembled and estimated read-only before any history mutation, summarization, or compaction
- **AND** no history is mutated, summarized, or compacted before that evaluation
- **AND** approved preparation may incur embedding work whose token accounting is out of scope, and action or summary generation, conversation mutation, and graph accounting occur only after the candidate fits
- **AND** preparation is not blanket-forbidden and the same existing budget cases, not a new algorithm, decide the outcome

#### Scenario: Larger-switch comparison uses the preceding selection, not the original
- **GIVEN** the operator manually switched main from an initial selection A with effective hard input budget `B=100,000` to a smaller B with `B=10,000`
- **AND** the first request under B was oversized and rejected so the guard for B remained
- **WHEN** the operator manually switches to a selection with effective hard input budget `B=20,000`
- **THEN** the larger-switch comparison tests the new selection's `B=20,000` against the preceding selected B (`B=10,000`), never the initial A (`B=100,000`), and the ordering decides whether the guard for B ends
- **AND** documented `C` values of 100,000, 10,000, and 20,000 with the same reserve order identically under the existing budget cases; when neither `C` nor `T` is known, labelled assumed local `C` supplies the combined-context input to those cases, and the comparison still uses the resulting `B`, not raw local `C`

#### Scenario: Input-only selections compare by their input budgets
- **GIVEN** combined `C` is unknown for both selections and the preceding selection documents `T=10,000`
- **AND** a smaller-switch guard is active for the conversation's selection
- **WHEN** the operator manually switches to a new selection documenting `T=20,000`
- **THEN** the existing input-only budget case makes the effective hard input budgets `10,000` and `20,000`, so `20,000 > 10,000` makes the switch larger without any raw-`C` comparison
- **AND** no `T-O` subtraction or new budget algorithm is introduced, and each selection keeps its own R6 ticket isolation

#### Scenario: Checkpoint resume keeps the guarded ticket and budget
- **GIVEN** a conversation was checkpointed while its guard for selection B, with the full R6 ticket identity, was active
- **AND** the current main selection is now another model
- **WHEN** an explicit resume of that checkpoint prepares a request
- **THEN** the resumed conversation keeps B's guard ticket and validates against B's exact budget cases
- **AND** it cannot borrow the current main selection's exemption, and resuming does not change the current main selection

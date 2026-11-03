# per-model-context-window Delta

## Purpose

Baseline: canonical `openspec/specs/per-model-context-window/spec.md` as already changed by the pending predecessor delta `openspec/changes/provider-model-catalog/specs/per-model-context-window/spec.md` (implementation baseline `a57ab26`). The predecessor modifies `Per-model context window awareness`, removes `Vision routing via all-models scan` and `Fallback models deprecation`, and adds the other requirement names removed below. This delta MUST be synchronized only after that predecessor delta. Because each affected contract is reversed rather than refined, it removes the predecessor's budgeting, fit, guard, fallback, and vision-gate requirements and adds informational counting, one compaction threshold, a narrow required output cap, and success-only compaction.

Final canonical Purpose (handoff for a later **authorized predecessor-then-follow-up sync**; do not edit the canonical spec during this batch): "Define model-independent estimated-input compaction using a configurable threshold, success-only automatic and manual summary publication, required-adapter output caps, and pinned-model image requests without catalog-based gating or request-size budgets."

## REMOVED Requirements

### Requirement: Per-model context window awareness

**Reason**: Per-model context windows, completion reserves, and hard input budgets no longer decide compaction or sends.

**Migration**: Replaced by `Compaction uses one model-independent threshold`. Replace `agent.ctx_max_tokens` with `agent.compaction_threshold_tokens`.

### Requirement: Obsolete fallback settings never create model fallback

**Reason**: Per-job `fallback_models` is no longer ignored at execution; it becomes a contained job configuration error.

**Migration**: Replaced by `Fallback settings are rejected and never create model fallback`; remove `fallback_models` from jobs.

### Requirement: Request budget limits are validated strictly

**Reason**: No request budget is computed, so metadata limits are not validated or used.

**Migration**: None required; limits shown by `/models` are advisory display data.

### Requirement: Fixed-output graph budgeting preserves its named exception

**Reason**: Graph extraction keeps its fixed wire values but loses its budget, reserve, and output-limit checks.

**Migration**: Replaced by `Graph extraction keeps its fixed wire policy without a fit gate`; oversized graph requests fail through provider validation and existing graph failure handling.

### Requirement: Vision requests use the pinned run model

**Reason**: Discovered vision metadata no longer gates image requests.

**Migration**: Replaced by `Image requests use the pinned run target without metadata gating`; no operator action is needed.

### Requirement: A manual smaller-model switch keeps selection and guards oversized requests

**Reason**: Model-switch guards and their tickets depend on removed request budgeting.

**Migration**: None required. After switching models, oversized requests are sent and any provider context rejection is reported normally; operators may use `/reset` or the hidden `/compress`.

## ADDED Requirements

### Requirement: Compaction uses one model-independent threshold

The compaction trigger SHALL be `agent.compaction_threshold_tokens`, a positive integer of estimated input tokens with default `60000`, compared with the existing estimator's total for system prompt, chat history, and current tool definitions. No model metadata, context window, completion reserve, or output limit SHALL change the threshold or decide whether a request is sent, and no request SHALL be rejected locally for estimated size.

#### Scenario: Default threshold applies
- **GIVEN** `agent.compaction_threshold_tokens` is not configured
- **WHEN** the threshold is resolved
- **THEN** it is `60000` for every pinned model

#### Scenario: Configured threshold is validated
- **GIVEN** `agent.compaction_threshold_tokens` is `0`, negative, a boolean, a float, or a string
- **WHEN** configuration is validated
- **THEN** startup fails with an actionable error naming the key and requiring a positive integer

#### Scenario: Model metadata does not change the threshold
- **GIVEN** two pinned models whose cached display data report different context sizes
- **WHEN** each run resolves its compaction threshold
- **THEN** both use the same configured value

#### Scenario: Tool-definition tokens included in the estimate
- **GIVEN** tool definitions consume 18,000 estimated tokens
- **WHEN** the request estimate is compared with the threshold
- **THEN** the estimate is system prompt tokens plus chat history tokens plus 18,000

#### Scenario: Estimated size never blocks a send
- **GIVEN** the estimate exceeds the threshold and no eligible middle can be compacted
- **WHEN** the request is prepared
- **THEN** the unchanged request is sent to the pinned model without local size rejection

### Requirement: Provider context rejections are not repaired automatically

A provider context-length rejection SHALL propagate through the existing error classification and checkpoint path. No forced compaction, truncation, or automatic resend SHALL follow it.

#### Scenario: Provider context rejection is classified
- **GIVEN** the provider rejects a request for context length
- **WHEN** the error is handled
- **THEN** it is classified `context` through the existing error path and checkpoint behavior
- **AND** no forced compaction, truncation, or automatic resend occurs

### Requirement: Required output cap is sent only where the adapter requires it

`agent.required_output_cap_tokens` SHALL be a positive integer with default `1024`, sent as the output-token parameter only for adapters whose API requires one, currently `anthropic`. Requests for other adapters SHALL omit optional output caps, `temperature`, and `top_p`. The value SHALL NOT be sized from context, discovered limits, or the input estimate; graph extraction's fixed cap is separate.

#### Scenario: Anthropic request carries the configured cap
- **GIVEN** a run is pinned to an `anthropic` connection and the setting is unset
- **WHEN** an ordinary request is sent
- **THEN** the wire payload carries `max_tokens = 1024` regardless of input size or discovered limits

#### Scenario: Optional adapters omit generation fields
- **GIVEN** a run is pinned to an `openai`, `openrouter`, `google`, or `ollama` connection
- **WHEN** an ordinary chat, native-tool, or JSON request is sent
- **THEN** the payload contains no output cap, `temperature`, or `top_p`

#### Scenario: Invalid cap setting is rejected
- **GIVEN** `agent.required_output_cap_tokens` is `0`, negative, a boolean, or a string
- **WHEN** configuration is validated
- **THEN** startup fails with an actionable error naming the key

### Requirement: Fallback settings are rejected and never create model fallback

Agent-level fallback settings SHALL be rejected with an actionable configuration error. A per-job `fallback_models` setting SHALL be a contained job configuration error as defined by `scheduled-job-execution`, never a startup error and never ignored. Every LLM client SHALL remain single-model for its run.

#### Scenario: Agent-level fallback setting is rejected
- **GIVEN** human configuration contains `agent.fallback_models` or `agent.fallback_model`
- **WHEN** configuration is validated
- **THEN** validation fails with an actionable error and the value is not used for selection

#### Scenario: Per-job fallback is a contained job error
- **GIVEN** a scheduled job carries `fallback_models`
- **WHEN** the scheduler loads and the job fires
- **THEN** only that job fails with an actionable configuration error and the document is not rewritten
- **AND** no cross-model retry is enabled

#### Scenario: Pinned client is single-model
- **GIVEN** an `LLMClient` is pinned to one run target
- **WHEN** a transient provider error occurs
- **THEN** no other model is tried

### Requirement: Image requests use the pinned run target without metadata gating

All image requests, including `vision_query`, SHALL use the admitted run target. Discovered or unknown vision metadata SHALL NOT gate them. When the pinned adapter cannot encode images the request SHALL fail locally with an actionable error; otherwise a provider rejection SHALL be a normal failure. No model scan, request-time switch, or `vision_query` model override SHALL exist.

#### Scenario: Unknown or false discovered vision does not block the request
- **GIVEN** the pinned model's cached display data reports vision as false or unknown
- **WHEN** a direct image or `vision_query` request is prepared for an adapter that can encode images
- **THEN** it is sent to the pinned model and image estimates count toward the compaction estimate

#### Scenario: Provider rejects an image
- **GIVEN** the provider rejects the image request
- **WHEN** the error is handled
- **THEN** it is a normal failure and no other model or endpoint is tried

#### Scenario: Adapter cannot encode images
- **GIVEN** the pinned adapter has no image encoding
- **WHEN** an image request is prepared
- **THEN** it fails actionably before provider I/O without scanning models

### Requirement: Graph extraction keeps its fixed wire policy without a fit gate

Graph extraction requests SHALL carry fixed `temperature=0.1` and `max_tokens=1024`, the named exception to omitting generation fields, subject to existing adapter compatibility stripping. No startup or per-extraction budget, reserve, or output-limit check SHALL run, and a provider rejection SHALL use existing graph failure handling with no substitute model and no reduced cap.

#### Scenario: Fixed values reach the wire
- **GIVEN** graph extraction runs on an adapter that accepts sampling fields
- **WHEN** the request is sent
- **THEN** it carries `temperature=0.1` and `max_tokens=1024`

#### Scenario: Large graph input is not preflighted
- **GIVEN** a graph extraction input is large
- **WHEN** the extraction is attempted
- **THEN** the request is sent without a local fit check
- **AND** a provider rejection fails only that extraction through existing graph failure handling

### Requirement: Automatic compaction attempts once per over-threshold request

Before each over-threshold model request, while automatic compaction is enabled for the run, the system SHALL make one attempt with the existing span partitioning and summary prompt. The goal and the last two messages, expanded to complete native tool-call spans, SHALL stay unchanged and only the eligible middle SHALL be summarized; an empty middle SHALL change nothing. A result still over the threshold SHALL be sent without another attempt in that step.

#### Scenario: Recent native group is kept whole
- **GIVEN** the second-to-last message is a tool result whose assistant tool call precedes it
- **WHEN** compaction partitions the history
- **THEN** the assistant tool call and all its results are retained together unchanged

#### Scenario: Empty eligible middle does nothing
- **GIVEN** the estimate exceeds the threshold but only the goal and recent spans exist
- **WHEN** compaction is considered
- **THEN** no summary call is made and the history is unchanged and not truncated

#### Scenario: Still over threshold after success
- **GIVEN** a successful compaction leaves the estimate above the threshold
- **WHEN** the current request is sent
- **THEN** no second compaction runs in that step
- **AND** the next over-threshold request makes a new attempt whose eligible middle can include the earlier summary message as ordinary history

### Requirement: Compaction publishes only a validated non-empty summary

A compaction candidate SHALL be built on a copy and published as the new history and goal index only when the summary is non-empty after stripping whitespace and the candidate passes native group validation. The goal, retained messages, and summary SHALL NOT be capped or truncated, and no deterministic fallback summary, short-context capping, or tightening loop SHALL exist.

#### Scenario: Successful compaction replaces only the middle
- **GIVEN** the history has a goal, several middle steps, and two recent messages, and the estimate exceeds the threshold
- **WHEN** the summary call returns non-empty text
- **THEN** the goal and the recent complete spans are unchanged and the middle is replaced by one summary message
- **AND** the goal index points to the unchanged goal

### Requirement: Compaction summaries use the pinned client with their own attempt state

Each compaction summary SHALL be requested through the run's pinned client with its own existing retry attempt state and usage accounting, leaving the main request's attempt state and checkpoint unchanged.

#### Scenario: Summary uses the pinned client with its own attempt state
- **GIVEN** a run is pinned to one target
- **WHEN** a compaction summary is requested and retried after a transient error
- **THEN** it is sent through the same pinned client with its own retry attempt state
- **AND** the main request's attempt state and checkpoint are not changed by it

### Requirement: Automatic compaction failure preserves history and disables further automatic attempts for the run

When an automatic summary call fails, returns empty text, or yields an invalid candidate, the system SHALL keep the original history and goal index, log a sanitized warning, continue the original request, and disable automatic compaction for that run only. Cancellation during the summary call SHALL propagate as run cancellation and SHALL NOT be recorded or warned as a compaction failure.

#### Scenario: Summary call fails
- **GIVEN** the summary call raises a provider error after its own retries
- **WHEN** automatic compaction handles it
- **THEN** the original history is sent unchanged and a sanitized warning is logged
- **AND** later over-threshold requests in that run make no automatic compaction attempt

#### Scenario: Empty summary is a failure
- **GIVEN** the summary call returns only whitespace
- **WHEN** automatic compaction handles it
- **THEN** no history is replaced and further automatic attempts in the run are disabled

#### Scenario: Cancellation ends the run without a failure record
- **GIVEN** the operator cancels during a summary call
- **WHEN** cancellation is raised
- **THEN** the run ends through existing cancellation handling with history unchanged
- **AND** no compaction-failure warning or error classification is produced

#### Scenario: A new run re-enables automatic compaction
- **GIVEN** automatic compaction was disabled by a failure in a previous run
- **WHEN** a new run exceeds the threshold
- **THEN** it makes an automatic compaction attempt

### Requirement: Manual compression preserves history unless summarization succeeds

The hidden `/compress` command SHALL summarize short-term conversation with its own prompt and atomically publish one summary entry only after a non-empty, validated response. On failure or an empty or invalid response it SHALL preserve the original history and report failure, without truncation or deterministic fallback. It SHALL NOT use automatic compaction's goal/middle/recent partition or summary prompt, and manual failure SHALL NOT disable automatic compaction.

#### Scenario: Manual compression succeeds
- **GIVEN** the short-term conversation contains at least two messages and the operator is idle
- **WHEN** `/compress` obtains a non-empty validated summary using its conversation-summary prompt
- **THEN** the original short-term messages are atomically replaced by one compressed summary entry
- **AND** the operator is told compression succeeded and working memory remains unchanged

#### Scenario: Manual compression fails
- **GIVEN** the summary call fails or the response is empty, whitespace-only, or invalid
- **WHEN** the operator runs `/compress`
- **THEN** the original short-term history is unchanged and the operator receives a failure message
- **AND** no truncated replacement is published

#### Scenario: Manual compression refuses while busy
- **GIVEN** an interactive operation holds the command lock
- **WHEN** the operator invokes `/compress`
- **THEN** the command reports that it cannot compress while busy
- **AND** no summarization is invoked and the history is unchanged

#### Scenario: Idle compression owns its summary client
- **GIVEN** a previous run's pinned client has already been closed
- **WHEN** the operator invokes `/compress` while idle with a valid active selection
- **THEN** the command independently admits a summary client from the factory without borrowing the closed client or changing the active selection
- **AND** that owned summary client is closed after success, failure, or cancellation

#### Scenario: Manual summary admission fails
- **GIVEN** there is no active selection and no summary client can be admitted
- **WHEN** the operator invokes `/compress`
- **THEN** the command reports an actionable failure and leaves the short-term history unchanged
- **AND** it does not borrow a prior run's client or disable automatic compaction for a future run

### Requirement: Manual compression owns its independently admitted client

Manual `/compress` SHALL obtain a separately admitted, owned summary client from the factory and close it after success, failure, or cancellation. An idle command SHALL NOT borrow a completed run's pinned client or require a live run pin.

#### Scenario: Manual summary client closes on failure
- **GIVEN** the operator invokes `/compress` while idle and summary admission succeeds
- **WHEN** the summary call fails or returns empty text
- **THEN** its owned client is closed and the original short-term history is unchanged

## Context

The current `_handle_llm_error` first excludes cancellation, non-retryable errors and headless runs, then writes an optional checkpoint and blocks in `ConfirmationManager.request_retry`. The latter owns retry-specific event/result maps and a default 120-second wait. A failed save still permits in-memory retry. Telegram callback delivery and run cleanup consequently share the lifetime of a failed request.

`AgentController._run_with_checkpoint_owner` already creates a client from the current active selection, acquires recovery ownership, validates a format-2 checkpoint and restores `_LoopState` plus `CallAttemptState`. It then rejects a different model ID. Removing that equality check changes continuity policy, not the storage format or the need for saved-history validation.

ADR inventory: all 48 numbered files in `adr/` were read and status/supersession declarations inspected. Relevant live constraints are ADR-0021 as partially superseded by ADR-0048 (checkpoint eligibility/persistence), ADR-0035 (logical-request budgets), ADR-0041 (single exact-target admission, partially superseding ADR-0034/0036/0038), ADR-0033 (fresh execution ownership), and ADR-0027 (shared tool-confirmation ledger). ADR-0024 is fully superseded by ADR-0027 and is historical context, not a live supersession target. Older superseded selection/catalog authority must not be restored. This change needs a new partial supersession of ADR-0021/0048's live recovery policy, not edits to those records.

## Goals / Non-Goals

**Goals:**
- One durable recovery route, `/resume`, with no live LLM retry wait.
- Continue saved work on the currently selected model, including across providers.
- Preserve retry boundedness, history validation, completed results, exact-snapshot retirement, cancellation and resource ownership.
- Report recovery availability honestly and announce a changed model/provider handoff.

**Non-Goals:**
- New discard commands, checkpoint migration/expiry, Resume buttons or recovery for headless runners.
- General lossless provider-history conversion, automatic model fallback, task restart or exactly-once execution.
- Changes to tool confirmation, step extension, deferred messages generally, scheduler policy, catalog metadata, model-selection buttons or Ollama transport.

## Decisions

### 1. Separate checkpoint eligibility from live interaction

Replace `interactive_llm_recovery` with a per-execution `checkpoint_recovery_eligible: bool = False` on controller invocation and ReactContext wiring. Telegram root/resume explicitly enable it. Runtime-built on-demand, scheduled, plan and diagnostic executions keep false, even when they have notification callbacks or tool-confirmation bridges. The global `checkpoint_enabled` setting remains independent: only an eligible run with writing enabled can create a new error checkpoint.

Do not infer eligibility from depth, callbacks, labels or checkpoint presence. Removing the capability entirely would accidentally extend persistence to headless failures; retaining its old name would misleadingly imply operator waiting.

### 2. Terminal failure saves and returns, never waits

Keep the existing automatic request policy and category classifier. `_handle_llm_error` checks cancellation first. For eligible retryable failures, atomically save the same format-2 loop/attempt payload through the existing owner-aware store. In the production owner-backed path, advertise recovery only when `CheckpointStore.save(...)` returns true AND `CheckpointStore.recovery_available(trace_id, owner)` returns true: that check rereads the supported envelope and compares it to the owner's exact expected snapshot. File existence or successful replace alone is insufficient. Ownerless test/compatibility paths must not weaken the production guarantee.

Return the existing fixed, sanitized category guidance immediately, with:
- verified save: “Saved progress is available. Use /resume to continue.”
- disabled/failed save for an eligible retryable request: “Progress could not be saved for recovery. This request cannot be resumed.”
- headless or non-retryable failure: existing terminal guidance without offering new recovery.

“Immediately” means no new operator-input wait; normal bounded child draining, client close, registration cleanup and lease release still run. Do not bypass lifecycle safeguards to make return faster. No memory-only retry survives.

Cancellation creates no new checkpoint. A pre-send resume preparation failure keeps the original checkpoint unchanged, even if the new target reports a non-retryable preparation problem. Do not replace a retained snapshot merely to record new model identity or error information.

### 3. Active-model resume is a new immutable execution

Keep `/resume` selection/authentication/busy checks and controller-owned eligibility. The run-client factory snapshots the active selection once using ordinary admission; do not use the historical `model_id` as a selector, fallback or cache gate. No active/admissible model means local actionable failure with no provider send and no snapshot mutation.

Remove only model-ID equality enforcement, not validation of the saved identity, trace, format, fields, messages, tool-call/result pairing, loop indices, eligibility or attempt state. Continue to require a valid admitted target and a valid historical model string. Preserve all fields/counters when restoring state; grant at most the existing single explicit continuation and never replenish automatic attempts.

Use the newly admitted target's normal adapters and run-local catalog view. Later `/models` changes affect only later admissions, never this resumed execution. Current selection and preference are not changed by resume. Newly saved recovery state after an actual failed send records the new execution's model identity; until that send, the old checkpoint is immutable historical evidence.

### 4. Announce handoff without adding a second approval flow

After eligibility/history validation and successful admission, but before sending saved context, emit an informational progress notice if canonical model IDs differ:

`Resuming with B; saved progress was created with A. Saved conversation and tool results will be sent to the selected model's provider.`

Use existing safe rendering/redaction for both identities; never expose credentials, endpoints, raw checkpoint contents or provider error bodies. Same-model resume keeps the ordinary resume notice. The notice does not promise that an identical model ID means the original client/configuration was restored. No additional approval or blocking prompt is introduced.

The controller owns the comparison so non-Telegram callers with progress callbacks observe the same semantics; Telegram renders it through existing progress delivery, after the ordinary goal/step resume notice and before model I/O. Notification failure must never trigger replay, second admission or a model switch. Existing notification resilience remains independent of execution.

### 5. Reuse conversion; preserve the actual-send boundary

Saved native turns use the shared history representation. Existing adapter serialization handles supported targets; existing native-to-JSON linearization can represent completed native calls as conversation without executing them. Keep the saved attempt mode/state through local conversion and preserve no-send accounting.

This is not a promise of universal or lossless compatibility: context length, images, tool payloads and target-provider restrictions can reject the request. Do not add metadata-based admission gates, error-triggered fallback, invented tool results, whole-run replay or automatic history rewriting in storage. A local preparation failure preserves the checkpoint; a real outbound request retires the exact snapshot immediately before transport send. A subsequent eligible retryable failure may save a fresh checkpoint, while a non-retryable post-send failure does not resurrect the retired snapshot.

Existing ownership is process-local, not a distributed lock or exactly-once guarantee. Preserve exact-snapshot checks, fail-closed retirement and refusal of concurrent/stale recovery. A model change does not weaken those guards.

### 6. Remove only the obsolete retry-specific surface

Delete retry event/result maps, request/signal methods, retry prefix publication, controller retry forwarding, live card renderer and timeout wiring. Retain tool-confirmation, headless bridge, standing grants, step-extension and general deferred-message machinery.

Keep only a small authenticated handler for old LLM retry/cancel callback payloads: acknowledge as expired and direct to `/resume`; never continue, delete a checkpoint, authorize an attempt or claim “Retrying”. Do not construct or register new live tokens. Stale callbacks are not a new resume route.

Remove `retry_timeout_seconds` from typed configuration/runtime dependencies and examples. Presence of `llm_error_handling.retry_timeout_seconds` with any value is rejected through the existing removed-field validation pattern, naming that field and instructing its removal because blocking retry cards were removed. Retain `checkpoint_enabled=true` by default. Existing eligible files can still be resumed when new checkpoint writing is disabled.

Supersede ADR-0021's live-card lifecycle clauses (delete on button Cancel; survive card timeout) with save-and-return plus explicit durable resume. There is no new discard control. Existing safe retirement on completion/send remains; abandoned files are operator-managed without new cleanup infrastructure. Remove obsolete retry-only entries in `vulture_whitelist.py` with the corresponding API removals.

### Component and data-flow view

User-confirmed presentation: lightweight C4-inspired ASCII component view of the existing single Python process, showing only changed recovery relationships; not a deployment diagram.

```text
Operator
   |
   | task / /resume N
   v
+----------------- Python process ----------------------------+
| Telegram commands --> AgentController --> ReAct loop         |
|                           |                  |               |
|                           | restore          | save + return |
|                           v                  v               |
|                       CheckpointStore                        |
|                           |                                  |
| Active selection --> run-client factory --> pinned client    |
|                                              |               |
|                    exact-snapshot retirement | before send   |
+----------------------------------------------|---------------+
                                               v
                                       Selected provider
```

- Telegram selects eligible snapshots; controller validates/restores them and owns per-execution client cleanup.
- The factory admits the active model once; adapters send saved context only to that target.
- The loop saves eligible failures and returns; no arrow enters a retry coordinator.
- CheckpointStore remains responsible for atomic writes, trace ownership and exact-snapshot retirement.
- Provider delivery and progress delivery remain separate; failed notification never re-executes work.

## Risks / Trade-offs

- Lost memory-only recovery -> explicitly accepted; fixed guidance must not promise unavailable recovery.
- Different model sees saved data or interprets it differently -> announce handoff, retain normal admission/credential isolation and never silently fallback.
- Context/native-history incompatibility -> use existing conversions; sanitized local/provider failure, no universal compatibility claim.
- Compact native-to-JSON conversion may abbreviate arguments -> document existing limitation; no new conversion framework.
- Abandoned checkpoints remain listed -> accepted operator-managed cleanup; no automatic deletion or new discard API.
- Obsolete config breaks startup -> actionable field-specific rejection and updated examples/docs.
- Prompt-scope ownership must not revive across trace reuse -> preserve fresh execution/turn/grant ownership and existing child draining.

## Migration Plan

Implement in focused red/green increments on an approved implementation branch after proposal reaches main. Update config examples and recovery documentation together with behavior. Keep format-2 checkpoints readable under existing strict validation, excluding only model equality; no rewrite or bulk cleanup.

Validate runtime behavior with deterministic transport-backed tests, not mocked retry waits alone. Cover cross-provider/native-to-JSON continuation, zero repeated tool executions, exact attempt accounting, one admission, selection changes after admission, save failure, cancellation, pre-send snapshot preservation, post-send retirement and owned-resource cleanup. Run required static checks and complete applicable suite.

Deploy after removing the obsolete timeout key from local configuration. Rollback restores previous code/configuration but does not undo completed provider/tool effects; never restore retired snapshots to “repair” a rollback. Old buttons remain inert in the new version. No automated data migration or rollback replay.

## Open Questions

No blocking product questions. The ADR stage must record the partial supersessions described above. Implementation should retain existing fixed-label failure handling and normal notification resilience rather than introducing a new recovery service or translation layer.

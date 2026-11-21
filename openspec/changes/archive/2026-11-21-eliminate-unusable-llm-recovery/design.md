## Context

`_handle_llm_error` currently saves before checking eligibility and always invokes `request_retry`. A headless runner has no interactive progress handler, yet the loop supplies a callable wrapper, so callback presence cannot establish interactivity. Telegram alone rejects non-retryable resume; direct controller callers bypass that policy.

ADR inventory and supersession headers were inspected. Relevant live constraints are ADR-0021 (disk recovery), ADR-0035 (logical-request retry state), ADR-0033 (execution ownership/drain), and ADR-0005/0007 (supervision/construction separation, with ADR-0036/0041 amendments). This design partially supersedes ADR-0021's unconditional error checkpointing and visibility, not persistence itself. Other constraints remain.

## Goals / Non-Goals

**Goals:** no unanswerable LLM retry waits or new unusable checkpoints; one authoritative resume policy; truthful terminal outcomes; preserve useful retryable recovery.

**Non-Goals:** new configuration/dependencies, result-protocol redesign, error reclassification, summary retry changes, tool-confirmation changes or legacy-file cleanup.

## Decisions

### 1. Explicit per-run capability

Add a boolean run argument, default false, carried through controller invocation into `ReactContext`. Telegram explicitly enables it for root requests and resume. `SubAgentRunner` leaves it false for on-demand, scheduled, plan-step and diagnostic runs, even if they can deliver notifications or bridge sensitive-tool confirmations. No mutable controller-wide toggle.

Alternative callback inference is smaller but conflates logging with usable operator controls. Removing all live recovery would exceed scope.

Lightweight component sketch (proposed; all components remain in the existing process):

```text
Telegram request/resume --> Controller --> ReactContext --> Error handler
                              |                               |
Startup / Telegram lists --> Recovery eligibility             +--> terminal failure
                              |                               |
                         Checkpoint store <-------------------+--> eligible retry
Headless runner ----------> Controller (capability=false)
Terminal runner result ---> Supervisor / Plan / Scheduler outcome
```

The controller enforces eligibility; adapters only present it. The store retains storage validation and ownership. No new runtime service is introduced.

### 2. Decide before saving or publishing a retry token

| Condition, in precedence order | Outcome |
| --- | --- |
| Cancellation signalled | Existing `[Cancelled]` outcome; no new error checkpoint/card |
| `context` or `permanent` (`retryable=false`) | Immediate fixed guidance; no new checkpoint/card |
| Capability false; `timeout`, `connection`, `rate_limit`, `empty`, `unknown` | Immediate sanitized terminal failure after existing automatic policy; no new checkpoint/card |
| Capability true; retryable category above | Existing Retry/Cancel wait and optional disk checkpoint |

Automatic retries stay in `_request_turn`; eligibility changes neither categories nor attempt budgets. Unknown remains operator-retryable, not automatically made transient. All terminal LLM failures start with `❌`, retain safe category wording for existing classification, and use fixed category-selected guidance. Context guidance recommends reducing request/context; permanent guidance recommends checking model/provider configuration; transient exhaustion reports exhaustion only when it actually occurred. Never interpolate provider text or promise resume when absent.

### 3. Recovery-layer resume policy

Use one small recovery/controller-layer predicate for otherwise valid checkpoint metadata: an explicit `retryable=false` disallows resume; omitted legacy metadata follows existing validation/default behavior. Expose an eligible-list view without changing raw store enumeration. Startup and Telegram use it, including consistent filtered numbering; controller resume enforces it again before manual authorization or transport send.

Old non-retryable files remain untouched, do not produce resume announcements and are not shown as resumable. Direct attempts get fixed non-retryable guidance. No deletion interface or migration. Malformed checkpoints retain existing validation/handling.

### 4. Preserve lifecycle and report failure accurately

Keep exact-snapshot ownership, lease release, client close, child drain, and send-gate retirement. Returning a new failure must not unconditionally retire an older retryable snapshot: preparation failure/cancellation before send preserves it; existing retirement immediately before a new outbound send remains authoritative.

At supervisor and plan result boundaries, narrowly recognize runtime terminal `❌` failures after cancellation handling, recording failed rather than successful completion. Preserve existing successful text/JSON results. Plans receive a non-recoverable failure so their recovery machinery cannot replay the task. Scheduler keeps existing failure classification, result logging, notification policy and next-cron behavior. No whole-run retry or new result envelope.

The existing `❌` convention reserves that prefix on unstructured terminal strings for failure (including other existing runtime early failures); it cannot simultaneously represent successful plain text. At supervisor/plan normalization, cancellation wins, existing explicit structured success/failure is honored, then the plain-string prefix is checked. An explicit successful JSON outcome whose output begins with `❌` remains successful. Plan failures use the existing outcome shape with `success=false`, `recoverable=false`; they do not enter retry or diagnostic-recovery replay.

## Risks / Trade-offs

- Lost state for terminal non-retryable failures -> intentional: unchanged requests cannot resume; guidance must not imply no tools ran.
- Capability omitted by a caller -> fail headlessly; cover Telegram root/resume wiring and real runner integration.
- Legacy files become invisible -> retain bytes, document manual operator cleanup as outside scope.
- Cancellation/data-loss races -> preserve ownership/send-gate paths; regression-test pre-send preservation and cleanup.
- Failure-string convention -> narrowly use existing runtime prefix, test successful outputs and cancellation; defer typed-result redesign.

## Migration Plan

No checkpoint format or config migration. Existing retryable files remain usable; existing non-retryable files become ineligible without deletion. Deploy ordinary code/docs changes; rollback restores former presentation/wait behavior against unchanged files.

## Open Questions

None blocking. ADR step must record partial supersession of ADR-0021: checkpoint/card eligibility and visibility only. API/helper names are implementation details.

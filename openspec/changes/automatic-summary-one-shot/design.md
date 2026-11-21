## Context

Automatic compaction currently calls `_summarize_on_pin` through the `maybe_compact` summary callback. That helper allocates an independent `CallAttemptState` using the main retry settings, retries transient summary failures, and waits between attempts. The existing `_prepare_request` failure path already preserves history, logs sanitized labels, and disables automatic compaction for the run.

The canonical `per-model-context-window` specification explicitly permits summary retries and must change. ADR-0043 governs success-only compaction and separate summary retry state; ADR-0035 governs agent-owned attempt accounting. Giving that independent summary state a zero-retry budget preserves those ownership boundaries. No previously accepted ADR is edited.

The ADR inventory was read in full with supersession links assessed before drafting. Remaining live constraints include immutable admitted pins (ADR-0036/0041), exactly-once lifecycle accounting (ADR-0023), and actionable interactive recovery only (ADR-0048). None of these boundaries changes.

## Goals / Non-Goals

**Goals:**
- One outbound summary attempt per triggered automatic compaction, without summary backoff.
- Preserve separate accounting, cancellation precedence, safe publication, and run-local disablement.
- Keep later successful compaction opportunities available.

**Non-Goals:**
- Changes to main-request retries, checkpoints, manual `/compress`, provider adapters, threshold/partition rules, or summary prompts.
- New configuration, dependencies, clients, fallback summaries, or abstractions.
- Broadening supported summary failures to swallow unexpected programming errors.

## Decisions

### Simplify the existing helper

Keep `_summarize_on_pin(ctx, prompt)` and allocate `CallAttemptState(max_retries=0, retry_delay=0)`. Bind the existing cancellation event and this independent attempt state, then invoke `ctx.llm.chat` once with the existing one-user-message prompt. Preserve actual-send and usage accounting. Do not authorize manual continuation for an optional automatic summary.

Remove the loop, retry wait, delay selection, and transient classification from this helper. Remove `_is_transient_llm_error` after confirming it has no remaining callers. Merely setting retries to zero was rejected because it leaves unreachable retry machinery; dropping attempt binding was rejected because isolation and accounting remain required.

### Preserve cancellation and failure ownership

Keep the pre-call cancellation check, propagate `LLMCancelledError`, and check cancellation again after a reply and when a supported provider exception is caught. A cancellation racing a provider failure must become cancellation, not `CompactionFailedError`. Catch only the existing supported provider-error family for this race check, then re-raise; leave unexpected exceptions unchanged.

Keep `_summarize_automatic` and `_prepare_request` behavior unchanged:

| Result | Existing owner and outcome |
| --- | --- |
| Non-empty summary with valid candidate | `_prepare_request` publishes history and goal index together; later over-threshold requests can summarize again. |
| Supported provider error | `_summarize_automatic` wraps it; `_prepare_request` retains original history/goal, warns safely, disables automatic compaction for this run, and continues the main request. |
| Empty, whitespace-only, or non-string output | Same existing failure path, without a second summary send. |
| Invalid candidate | Same existing failure path, without a second summary send. |
| Cancellation before/during send, racing failure, or after reply before publication | Propagates unwarned; no replacement history or subsequent main send. |
| Unexpected programming error | Propagates unchanged. |
| Empty eligible middle | No summary call; existing history stays unchanged. |

The existing callback path and module responsibilities stay unchanged: `_prepare_request` calls `maybe_compact`, which calls `_summarize_automatic`, which invokes the `_summarize_on_pin` callback. The callback returns summary text or raises; candidate validation/publication remain outside it. A C4 diagram adds no useful boundary information for this single-helper change; no deployment or component redesign is proposed.

### Keep main requests independent

The summary's one attempt must not consume, refill, replace, authorize, or persist the main request's retry state or checkpoint. Following summary failure, the original main request retains its normal transient retry allowance. Do not add a one-summary-per-run success flag: only failure disables future attempts.

## Risks / Trade-offs

- [Transient summary errors lose a retry opportunity] -> Accept deliberately: send the preserved original main request immediately; retain the established run-local disablement policy.
- [Cancellation races accidentally become warnings or publication] -> Retain checks around the bound call and existing publication guard; test all four cancellation timings.
- [Tests mock away actual attempt accounting] -> Include a recording transport/client-path regression that distinguishes summary and main sends and verifies independent budgets with a nonzero configured main retry allowance.
- [Retry language remains misleading] -> Update both modified spec requirements in full and relevant current documentation; do not rewrite accepted ADRs or historical archives.

## Migration Plan

No data/configuration migration. Implement tests first, simplify the helper, then run focused compaction regressions and required static checks. Finish with the full applicable suite (`make check`) and the explicit repository-required unexcluded vulture command; report existing skips and any failures honestly. Rollback restores the helper and its earlier summary retry policy; no persisted state changes.

## Open Questions

None blocking. This is a scoped retry-budget tightening, not a new construction, ownership, persistence, or recovery architecture. ADR review will record the existing constraints and confirm whether any durable ADR is warranted.

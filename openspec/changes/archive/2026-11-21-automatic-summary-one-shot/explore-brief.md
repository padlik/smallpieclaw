# Exploration brief

## Agreed approach

Make automatic compaction summaries one-shot: at most one outbound summary attempt per triggered compaction, with independent zero-retry attempt accounting on the existing pinned client. Remove the summary retry loop, backoff waits, and its unused transient-classification helper. No new abstraction or configuration.

## Complete behavior mapping

| Outcome | History and run behavior |
| --- | --- |
| Valid non-empty summary | Publish the validated candidate; later over-threshold requests may summarize again. |
| Supported provider failure | Preserve original history and goal index, warn with fixed labels, disable automatic compaction for this run, continue the main request. |
| Empty/whitespace/non-string summary | Same existing compaction-failure behavior; no retry. |
| Invalid candidate | Same existing compaction-failure behavior; no retry. |
| Cancellation before send, during send, racing provider failure, or after reply before publication | Propagate cancellation; no replacement history, compaction-failure warning, or main send. |
| Unexpected programming error | Propagate unchanged; do not broaden failure containment. |
| No eligible middle | No summary send; preserve existing partitioning and threshold behavior. |

## Data flow and boundaries

`_prepare_request` -> `maybe_compact` -> `_summarize_automatic` -> callback `_summarize_on_pin(ctx, prompt)` -> pinned `ctx.llm.chat(messages)`. Keep cancellation binding and a separate `CallAttemptState(0, 0)` around the single summary call. Actual-send accounting and usage stay intact; the main request's attempt state and checkpoint stay untouched. Existing `_prepare_request` failure handling remains unchanged.

## Alternatives rejected

- Only set the existing retry budget to zero: works behaviorally, but leaves dead loop/backoff code.
- Remove attempt binding: loses explicit isolated accounting.
- One successful summary per run, later retries after failure, new configuration, manual `/compress` changes, and main-request retry changes: outside the agreed scope.

## Open questions

None blocking. Current `per-model-context-window` retry language must be changed explicitly. Validation must prove one failed summary send with zero waits despite a nonzero main retry budget, cancellation precedence, failure disablement, and later summarization after success.

## Planning progress

- [x] Proposal reviewed
- [x] Design reviewed
- [x] Delta specs reviewed
- [x] ADR condition evaluated
- [x] Tasks reviewed
- [x] Planning validation passed

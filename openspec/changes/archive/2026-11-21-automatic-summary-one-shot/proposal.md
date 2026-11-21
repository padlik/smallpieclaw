## Why

Optional automatic summarization currently consumes its own retry budget and backoff waits before the main request gets its own retry budget. Council finding #3 recommends a single summary attempt, relying on the existing safe failure path rather than delaying the actual task.

## What Changes

- Limit each triggered automatic compaction summary to one outbound attempt with no retry or backoff wait.
- Preserve the pinned client, separate summary attempt accounting and usage accounting, and cancellation precedence.
- Preserve original history and goal index on supported summary failure, empty output, or invalid candidate; retain sanitized warnings and run-local disablement.
- Allow later over-threshold requests to summarize again after successful compaction.
- Remove the summary retry loop and its unused transient-error helper.
- Leave main-request retries, checkpoints, manual `/compress`, partitioning, thresholds, and programming-error propagation unchanged.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `per-model-context-window`: Replace automatic summary retry semantics with one outbound attempt per triggered summary, preserving independent accounting and existing failure/cancellation contracts.

## Impact

- Production scope: `react_loop.py::_summarize_on_pin` and its now-unused transient classification helper; no new dependencies, configuration, or public API.
- Regression scope: automatic-compaction partition, publication, and failure tests, including main-request retry-budget isolation and cancellation races.
- Documentation: align existing automatic-compaction wording wherever it explicitly promises summary retries.
- Planning only in this change stage; implementation requires a separate apply request.

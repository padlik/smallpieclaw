# Council findings: model catalog and runtime simplification

Date: 2026-11-19

## Scope and evidence

Read-only council review of the latest `provider-model-catalog` and
`simplify-model-runtime` implementations, answering:

1. Is further simplification possible?
2. What functionality could be dropped?
3. Which recovery paths should simply report an error to the client?

The review targeted committed code on `main`, including the archived changes:

- `openspec/changes/archive/2026-11-17-provider-model-catalog/`
- `openspec/changes/archive/2026-11-19-simplify-model-runtime/`

Three independent seats reviewed source and relevant tests. Beta also reviewed
canonical specifications and archived artifacts; alpha and gamma could not
access the OpenSpec skills or CodeGraph in their sessions. No tests were run.
References below are review-time locations and may shift. Findings are
recommendations, not approved implementation tasks or verified fixes.

## Verdict

Further simplification is worthwhile. Start with obsolete production paths and
recovery that cannot help, rather than removing lifecycle or security safeguards.

## 1. Remove obsolete machinery first

| Candidate | Evidence | Qualification |
| --- | --- | --- |
| Retire old catalog-based graph preparation | `backfill_graph_memory.py:174–211,676–679`; `graph_memory.py:1527–1575`; `model_registry.py:473–514` | Live startup/backfill use explicit graph settings. Check public-helper compatibility before deleting the full chain. |
| Remove unused Google discovery descriptors | `model_discovery.py:39–65,125–136,363–375`; `model_registry.py:589–598` | Descriptors are constructed but not consumed by refresh. Keep raw-response credential scanning. |
| Retire old TOML catalog persistence and cache-only selection | `main.py:708–715`; `model_settings.py:59–172,538–642`; `model_registry.py:240–243,362–384,604–606` | Production uses `ModelCache` and `ModelPreference`. Preserve shared validators and atomic writes; expect substantial fixture migration. |
| Retire legacy mutable-client model selection | `telegram_commands.py:1500–1537`; `telegram_callbacks.py:179–204` | Unreachable through current production wiring. Removing compatibility APIs is an explicit decision; consider retaining a small stale-button response. |
| Remove unread tool-definition hashing and old retry scaffolding | `react_loop.py:1178–1182,1323–1331`; `llm_client.py:117–123,359–371`; `providers/_utils.py:88–148,192–277` | Hash has no runtime reader; retry stubs make one call. Removing unused diagnostic configuration still needs a compatibility policy. |

These are caller-traced opportunities, not a guarantee that every external
consumer or persisted contract is absent.

## 2. Recovery that should report failure instead

### Headless interactive retry waits

Sub-agent execution lacks an interactive progress callback, but terminal LLM
failures still reach `request_retry()`, whose default wait is 120 seconds.

- Runner: `agent_controller.py:1218–1231`
- Handler: `react_loop.py:830–832`
- Wait: `confirmation.py:341–364`

After bounded automatic retries, return a sanitized terminal failure immediately
to the parent or scheduler. Preserve cancellation and cleanup. Add a real
runner-path regression: existing retry tests mock away the wait.

### Non-retryable checkpoints and blocking cards

Permanent/context failures create checkpoints and enter a blocking recovery flow,
but `/resume` refuses those checkpoints. Startup still announces unfinished runs.

- Handler: `react_loop.py:775–832`
- Cancel-only card: `telegram_interface.py:1143–1146`
- Resume rejection: `telegram_commands.py:430–436`
- Startup notification: `main.py:1038–1046`

Report these failures immediately with fixed, actionable guidance. Do not create
an unusable resumable checkpoint. This changes the recovery specification.

### Invalid native history

Local history validation becomes a misleading provider-rejection message:
`context_manager.py:75–78`; `react_loop.py:743–744,1654–1660`.

Return a dedicated fixed-label local-history failure with corrective guidance.
Do not expose arbitrary exception text or retry unchanged invalid history.

### Automatic compaction summary retries

Optional summarization has a full retry budget before the main request receives
its own budget: `react_loop.py:1194–1226`. Summary failure already preserves the
original history: `react_loop.py:1259–1269`.

Make automatic summarization one-shot. Preserve cancellation, original history,
and the existing run-local disable-on-failure behavior. Allowing later attempts
after transient summary failure was suggested by alpha, but is not the initial
consensus recommendation.

### Unexpected programming faults

The broad exception path can turn unexpected faults into operator-retryable
recovery: `react_loop.py:1414–1421`.

Distinguish supported provider errors from unexpected faults. Log the latter
with traceback and return a fixed internal-error message. Changing the current
unknown-error policy requires an explicit specification decision.

### Retries worth retaining

There is one automatic task-request retry owner, `_request_turn()`
(`react_loop.py:1306–1421`), not a hidden SDK retry stack. Keep bounded transient
request retries. Normalize duplicate empty-response retry handling rather than
dropping its resilience without a product decision.

## 3. Optional capability cuts

- **Remove blocking live Retry cards while retaining `/resume`.** This is a
  deliberate recovery-experience change.
- **Reduce detailed catalog metadata repeated in prompts.** Retain model IDs,
  active/pinned identity and freshness; keep full metadata in `/models`.
  This reduces capability hints and is not behavior-preserving.
- **Reject structurally invalid scheduled model selectors at mutation time.**
  Keep launch-time validation for edited files; do not require catalog membership.
- **Consider direct HTTP for Ollama separately.** Potential adapter savings are
  untested; origin/auth, wire semantics and error handling must remain equivalent.

Do not casually drop durable resume: it preserves completed tool results without
replaying tools (`tests/test_run_recovery_contract_4_4.py:247–291`). Restarting a
task is not equivalent and can duplicate side effects.

Do not merely truncate `/models` selection options to simplify rendering.
Button-only selection makes the compact renderer useful. A typed escape hatch
would be an explicit reversal of the current selection contract.

## 4. Protections to retain

- Immutable run pins and admission before allocation.
- Credential scanning, safe error labels, origin/auth restrictions and
  cancellation precedence.
- Atomic cache/preference writes, bounded discovery, scoped selection tokens
  and concurrent-refresh ordering.
- Actual-send attempt accounting, checkpoint ownership and anti-replay retirement.
- Explicit transport ownership and idempotent close.
- Optional graph failure containment.
- Separation of execution from notification: failed delivery must not rerun work.

Keep catalog/selection revisions for now. Alpha questioned their live consumers;
beta recommended preserving them. Stored provenance is not automatically a
concurrency guard, but lack of branching consumers alone does not prove it is
disposable. Audit persisted and observability contracts separately.

## 5. Additional safety findings

These source-review findings were not tested and are not established as having
originated in the reviewed features.

1. **Rejected runner cleanup:** submission failure appears to deregister a
   constructed runner without closing its owned clients
   (`sub_agent_supervisor.py:419–447`; `agent_controller.py:1136–1147`).
   Establish explicit ownership transfer and close on rejection.
2. **Destructive graph recovery:** startup can delete WAL sidecars and lose
   committed, uncheckpointed transactions (`graph_memory.py:353–402`).
   Prefer preserving files, marking graph memory unavailable and reporting
   repair guidance. Data-loss repair should require operator approval.

## Recommended sequence

1. Remove obsolete production paths, preserving compatibility deliberately.
2. Eliminate headless waits and unusable non-retryable checkpoints.
3. Make automatic summarization one-shot.
4. Address runner ownership and destructive WAL recovery with focused regressions.
5. Separately decide whether live Retry cards and other optional capabilities
   justify their complexity.

No implementation, test execution, commit or merge was part of this review.

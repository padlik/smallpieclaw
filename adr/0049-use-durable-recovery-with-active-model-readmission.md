# Use durable recovery with active-model readmission

## Status

accepted, supersedes ADR-0021 and ADR-0048 (partial; see Supersedes)

## Date

2026-11-24

## Supersedes

ADR-0021 (partial: replace checkpoint-plus-blocking-Retry/Cancel coupling, delete-on-card-Cancel and survive-card-timeout lifecycle, and memory-only recovery after a failed save with save-and-return followed by explicit durable `/resume`). ADR-0048 (partial: replace live interactive LLM recovery with explicit durable-checkpoint eligibility, default false and enabled by Telegram root/resume only). Preserve atomic persistence, trace reuse, no automatic cleanup, headless/non-retryable exclusion, controller-owned eligibility, pre-send preservation and exact-snapshot retirement. ADR-0024 is already fully superseded by ADR-0027 and is not superseded again.

## Context

Blocking Retry cards retain failed execution resources and create a second recovery lifecycle beside durable `/resume`. Their extra value is memory-only continuation and retaining the original client; the operator accepts losing those capabilities. A resume already creates a new client, yet model-ID equality blocks an intentional handoff to a newly selected model when the former target is unavailable.

ADR-0021's remaining live-card lifecycle and ADR-0048's explicit interactive capability are therefore revisited. ADR-0035's attempt accounting, ADR-0041's single exact-target admission, ADR-0033's fresh execution ownership and ADR-0027's tool-consent boundaries remain in force.

## Considered Options

- Keep blocking cards: preserves memory-only recovery, but duplicates recovery state and retains failed-run resources.
- Replace cards with a new Resume button: keeps one-click access, but adds tokens, stale-control handling and another entry point.
- Durable `/resume` with historical-model equality: removes waiting, but obstructs deliberate model replacement.
- Durable `/resume` admitted on the active model: chosen; one explicit continuation route with an immutable target per execution.

## Decision

Eligible retryable failures save a checkpoint when enabled and end without a live retry wait. Offer recovery only after successful save and owner-backed exact-snapshot verification. Disabled/failed persistence provides no memory-only recovery. Headless and non-retryable failures remain ineligible for new checkpoints, and cancellation takes precedence.

`/resume` restores saved work and admits the current active model exactly once for the new execution, without enforcing historical model equality. Announce a changed model and saved-context provider handoff before sending, without another blocking approval. Keep that new target pinned; resume neither changes the active preference nor falls back to another model.

Preserve strict checkpoint/history validation, saved attempt counters, the existing single manual continuation authorization, no automatic-budget refill, no replay of completed tools, process-local ownership and exact-snapshot retirement before actual send. Pre-send failures preserve the original snapshot unchanged; normal target rejection fails safely rather than restarting. Existing adapter serialization/conversion remains, without a universal compatibility or exactly-once guarantee.

Remove retry-only signaling and timeout configuration. Reject the obsolete timeout key with removal guidance. Old card callbacks are inert; tool confirmation and general deferred messages remain independent. No new discard command or automatic cleanup is introduced.

## Consequences

- Good: failed runs release resources through normal cleanup without waiting for input; durable recovery has one continuation contract.
- Good: unavailable historical models can be replaced deliberately while preserving completed work and bounded attempt accounting.
- Bad: disabled/broken checkpoint storage means recovery is unavailable.
- Bad: saved context is sent to the selected provider, and target/history incompatibility can still fail; the handoff notice makes the change visible.
- Bad: abandoned checkpoints remain operator-managed; no new cleanup interface is added.
- Follow-up: implement and test these accepted planning decisions through the separately authorized apply phase; update configuration examples and recovery documentation.

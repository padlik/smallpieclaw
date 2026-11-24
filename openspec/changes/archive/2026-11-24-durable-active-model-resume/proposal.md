## Why

Blocking Retry cards keep a failed run, client and ownership lease alive while duplicating durable recovery through `/resume`. A separate same-model restriction prevents recovering saved work with a deliberately selected replacement model, even though resume already constructs a new client.

## What Changes

- **BREAKING**: Remove live LLM Retry/Cancel cards, retry waits and memory-only continuation. Eligible interactive retryable failures save a checkpoint when enabled, return sanitized failure and truthful recovery guidance, and release run-owned resources through existing cleanup.
- Keep `/resume` and `/resume N`, eligible-checkpoint filtering, startup notifications and saved completed tool results. Disabled or unsuccessful checkpoint writing provides no recovery for the failed request.
- Abandoned checkpoints remain until existing safe retirement or operator-managed file cleanup; adding a discard command, automatic expiry or garbage collection is out of scope.
- **BREAKING**: Resume with the active model admitted when recovery starts rather than enforcing equality with the checkpoint's historical model. Announce model changes and explain the saved-context handoff to the newly selected provider; pin the new target for that resumed execution.
- Preserve saved logical-request counters and existing single explicit continuation authorization, ownership, exact-snapshot retirement before send, cancellation precedence and client cleanup. Model switching neither replays completed tools nor replenishes the automatic budget.
- Replace the explicitly enabled interactive recovery capability with explicitly enabled durable-checkpoint eligibility, default false; Telegram root/resume remain eligible and headless runs remain ineligible independently of callbacks and tool confirmations.
- Reject obsolete `llm_error_handling.retry_timeout_seconds` at configuration validation with guidance to remove it because blocking cards were removed; stale card callbacks must not authorize continuation or mutate recovery state.
- Keep existing native-history serialization/conversion. An incompatible target fails normally, without automatic fallback or task restart; pre-send preparation failures leave recovery data unchanged.
- An unset or inadmissible active model fails locally with actionable selection/configuration guidance, sends nothing and leaves the checkpoint unchanged.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `llm-error-recovery`: Replace blocking cards with immediate durable-only recovery guidance; configure checkpoint writing without retry waits; define active-model resume, handoff notices and unchanged safety boundaries.
- `provider-model-catalog`: Replace checkpoint-model continuity in “Run ownership and recovery contracts are unchanged” with current-selection admission for each resumed execution, and clarify “Run admission pins one immutable target without fallback”; retain immutable per-execution pins, ownership and no fallback.

## Impact

- Runtime: `react_loop.py`, `agent_controller.py`, `confirmation.py`, `main.py` and their typed context/config wiring.
- Telegram: retry-card rendering/callback handling and resume presentation in `telegram_interface.py`, `telegram_callbacks.py`, `telegram_commands.py`; `/resume` remains discoverable with unchanged selection syntax.
- Configuration and documentation: `config_schema.py`, configuration examples and recovery usage documentation; obsolete `llm_error_handling.retry_timeout_seconds` needs an actionable removal policy.
- Tests: recovery capability, checkpoint lifecycle, active-model admission, native/JSON history continuity, actual-send retirement, retry budgets, stale callbacks and cleanup regressions.
- No new dependencies or checkpoint-format migration are intended. No changes to sensitive-tool confirmation, deferred messages generally, headless checkpoint eligibility, scheduler behavior, catalog prompt metadata, `/models` selection or the Ollama adapter.

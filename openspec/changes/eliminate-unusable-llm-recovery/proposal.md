## Why

Headless LLM failures currently enter an operator retry wait that nobody can answer. Context/permanent failures also create checkpoints and blocking cards even though Telegram refuses to resume them.

## What Changes

- End non-retryable failures and headless exhausted requests immediately with sanitized actionable errors, without new recovery checkpoints or cards.
- Explicitly enable interactive LLM recovery per run; preserve retryable interactive cards, bounded automatic retries and durable resume.
- Enforce resume eligibility in the controller/recovery layer, shared with Telegram presentation and startup filtering. Leave older non-retryable checkpoint files untouched and unused.
- Propagate terminal LLM failures accurately through supervisor, plan and scheduler outcomes without restarting completed work.
- Preserve cancellation, cleanup, checkpoint ownership, send-gate retirement and actual-send attempt accounting.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `llm-error-recovery`: Limit interactive and disk recovery to eligible requests; centralize resume eligibility and preserve failure outcomes across callers.

## Impact

Affected areas: `react_loop.py`, controller run/resume wiring, Telegram request/command/startup presentation, `sub_agent_supervisor.py`, `execution_plan.py`, recovery tests and user documentation. No dependencies, checkpoint-format migration or new configuration settings.

Not included: removing retryable recovery, changing error classifications or automatic retry budgets, summary retries, tool-confirmation behavior, deleting old checkpoint files, or a general result-protocol redesign.

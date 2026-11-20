# Exploration brief

Implement recommended sequence item 2 from `docs/model-runtime-council-findings.md`, not complete checkpoint removal.

- Explicit per-run interactive LLM recovery capability, default false; Telegram root request/resume enables it. Callback presence, notification delivery, depth and labels do not grant it. Tool-confirmation bridges are independent.
- Mapping: cancellation -> existing cancellation outcome; context/permanent -> immediate fixed actionable failure, no new checkpoint/card; headless timeout/connection/rate_limit/empty/unknown -> immediate sanitized failure after existing automatic policy, no new checkpoint/card; interactive retryable categories -> existing in-memory retry and optional durable recovery.
- Controller/recovery owns resume eligibility; Telegram consults the same rule for presentation. Valid stored checkpoints explicitly marked non-retryable are refused before manual authorization, excluded from startup announcements and filtered `/resume` numbering, and left untouched. Existing malformed-checkpoint handling remains.
- Flow: Telegram request -> controller run -> ReactContext capability -> error-handler decision before checkpoint save/request_retry. Startup and Telegram consume recovery-layer eligible listings; direct controller resume rechecks eligibility. Runner failure -> supervisor/plan/scheduler failure outcome, never whole-run replay.
- Preserve cancellation precedence, run/client cleanup, child draining, exact-snapshot ownership, send-gate retirement, attempt budgets and pre-send recovery data. Do not unconditionally delete existing retryable snapshots on a new preparation failure.
- Reject callback inference (not proof of operator control), global recovery removal (loses useful retryable continuation), and automatic legacy deletion (unnecessary destructive migration).
- Out of scope: summary retries, error reclassification, invalid-history behavior, tool confirmation, checkpoint migration/deletion UI, new result protocols.
- Open questions: none blocking. Legacy retention and explicit capability are proposed defaults supported by the exploration; implementation API naming is left to the implementer.

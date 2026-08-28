# 0024-unify-confirmation-signaling-in-coordinator

## Status

Accepted, supersedes ADR-0011

## Date

2026-08-28

## Supersedes

ADR-0011

## Context

ADR-0011 established per-prompt scope for operator approval grants and introduced `_prompt_approval_set` — a reference on `BuiltinExecutor` pointing to `ConfirmationManager.auto_approve_tools` — as the mechanism for sharing the approval set between the main agent (depth 0) and sub-agents (depth ≥ 1). ADR-0011 also established that `shell` is never auto-approved and that sub-agent shell is always blocked.

Two confirmation systems coexisted: `ConfirmationManager` (3 flow types for depth 0) and a separate headless bridge on `BuiltinExecutor` (1 flow type for depth ≥ 1), with entangled shared state (`_pending`, `auto_approve_tools` via `_prompt_approval_set`, and a race condition in `cb_subagent_confirm` where `auto_approve_tools` was mutated via direct field access separately from the event signal).

Three archived changes deferred the extraction, gated on a security review (resolved by nsjail shell isolation, ADR-0012) and a "unified approve-all" decision (resolved: extend the existing class rather than creating a new coordinator).

## Considered Options

- **Extend ConfirmationManager in-place** (chosen): Add `request_headless_confirmation()` and `signal_headless_confirmation()` to the existing class. Smallest diff, no import churn, same Event+dict+token pattern. Single-gate pattern per research.
- **New ConfirmationCoordinator class**: Wrap both systems in a new class. Rejected: indirection without behavioral benefit, doubles import surface, contradicts "one gate" principle.
- **Rename ConfirmationManager → ConfirmationCoordinator**: Rejected: import churn across 6+ files with zero architectural gain.

## Decision Outcome

`ConfirmationManager` SHALL own the signaling (Event + result dict + token lifecycle) for all four confirmation flow types: tool confirmation at depth 0, tool confirmation at depth ≥ 1 (headless bridge), step extension, and LLM error retry.

The `_prompt_approval_set` reference mechanism is replaced by a `_coordinator` reference on `BuiltinExecutor` pointing to the `ConfirmationManager` instance, set at run start and set to `None` at run end (fail-closed). The coordinator owns `auto_approve_tools` directly — no more indirect reference.

Sub-agent approve-all SHALL be atomic: `signal_headless_confirmation(token, approved, approve_all=False, tool_name="")` adds to `auto_approve_tools` AND sets the event in one method call, eliminating the race condition.

The coordinator is transport-agnostic: the Telegram prompt callback is passed at call time (`request_headless_confirmation(prompt_fn=...)`), not stored on the coordinator.

`BuiltinExecutor` retains tool staging (`_pending`), zone context (`_zone_paths`, `_zone_trackers`), and execution (`confirm`/`cancel`).

ADR-0011's decisions that are preserved unchanged:
- Per-prompt scope: grants expire at run end (`clear_auto_approve()` in `AgentController.run()` finally block).
- Shared set: one "Approve all file_read" covers the main agent and all sub-agents for the prompt.
- `shell` is never auto-approved for sub-agents (always-blocked regardless of any approve-all grant). The main-agent shell gate is configurable via nsjail confirmation mode (ADR-0012 amendment).
- Approve-all buttons are file-tool-only (`file_read`, `file_write`, `file_patch`).

## Consequences

- Good, because all confirmation signaling flows through one class — no more two-system entanglement or race condition on `auto_approve_tools`.
- Good, because the coordinator is transport-agnostic — future non-Telegram transports can supply their own prompt function at call time.
- Good, because the `_coordinator is None` fail-closed guard is simpler and more explicit than the `_prompt_approval_set is None` indirection.
- Bad, because an orphaned sub-agent after run end is now blocked without prompting (behavior change from ADR-0011's "re-prompt" behavior). This is arguably more correct — there's no live run context to deliver the prompt into.
- Neutral, because `ConfirmationManager` grows from 196 to ~236 lines (one new flow type). Acceptable within project conventions.
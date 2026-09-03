# 0027-single-grant-ledger-for-standing-consent

## Status

Accepted, supersedes ADR-0011, supersedes ADR-0024

## Date

2026-09-03

## Supersedes

ADR-0011: Per-prompt approval scope
ADR-0024: Unify confirmation signaling in coordinator

## Context

ADR-0011 established per-prompt approval scope and its invariant: `shell` is never auto-approved — always-confirmed for the main agent, always-blocked for sub-agents. ADR-0024 unified confirmation *signaling* in a coordinator and added `auto_approve_tools`. The C-16 review (2026-09) found the mechanism split had grown unsafe and inconsistent:

1. **Asymmetric enforcement**: the sub-agent path enforced `_ALLOWED_APPROVE_ALL_TOOLS` at the sink (shell rejected, crafted-callback-tested), but the main-agent `signal_approve_all` accepted *any* tool including `shell` — the highest-blast-radius operation had the weakest gating, reachable via a normal UI button.
2. **Four overlapping consent mechanisms** coexisted: one-shot tokens, per-tool `auto_approve_tools`, per-message `GrantTracker` request grants, and persistent directory trust — with different lifetimes, different granularity, and no shared policy.
3. A full `ConfirmationCoordinator` merge (ADR-0008's deferred item) was evaluated and rejected: the two objects have genuinely different responsibilities (staging/execution vs blocking/signaling), and merging would increase coupling at high concurrency risk.

## Considered Options

- **Full ConfirmationCoordinator merge**: rejected — coupling increases for cosmetic gain; the defects live in policy, not object layout.
- **Keep auto_approve_tools, add a main-agent allowlist**: rejected — still three consent mechanisms; per-tool grants without directory scope overshoot (approving one write approves writes everywhere for the task).
- **Single grant ledger**: one grant type `(tool, dir-recursive)` × lifetime {prompt, session}, sink-side tool vetoes, in-memory only.

## Decision Outcome

All standing consent moves into a single `GrantLedger` owned by the depth-0 `ConfirmationManager` (the `_coordinator` bridge delegates to it; it is not a second ledger). A grant is `(tool, dir, lifetime, scope_owner)` where `dir` covers the directory recursively. Two lifetimes only: **prompt** (cleared at `react_loop()` entry) and **session** (cleared by `/reset`, which clears both). Confirmation prompts for file ops render three buttons: Confirm (prompt-lifetime grant), Till /reset (session-lifetime grant), Deny. `shell` and `secret_get` are vetoed at the ledger check (`may_hold_grant(tool) -> False`) — enforced at the sink, not the Telegram boundary — and their prompts render Confirm/Deny only, preserving ADR-0011's shell invariant with stronger, boundary-independent enforcement. Prohibited paths are immune to grants by check ordering (classification runs before the ledger). Sub-agent prompt-lifetime grants carry a scope annotation and expire with the sub-agent's run — no upward privilege leak. Grants are never persisted.

## Consequences

- Good: the main-agent shell bulk-approve hole closes structurally (veto at the only sink); crafted-callback defense is inherent (sink enforcement), matching the already-tested sub-agent pattern.
- Good: `auto_approve_tools`, `_ALLOWED_APPROVE_ALL_TOOLS`, `GrantTracker`, and zone-button grants are deleted — one mechanism replaces four.
- Good: `(tool, dir)` granularity localizes consent to the directory the operator actually saw in the prompt.
- Bad: no pure one-shot for file ops (Confirm also grants the dir for the rest of the prompt cycle) — accepted deliberately to cut the 93%-approval-fatigue re-prompting; operators who want one-shot can Deny and re-request.
- Bad: session grants last until `/reset` — a longer standing authorization than the old per-prompt approve-all; mitigated by strictly narrower scope (one tool + one dir).
- Follow-up: token staging lifecycle (`PendingConfirmations` with lock and run-scoped `reset()`) lands with this change as supporting infrastructure.
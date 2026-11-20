# Limit LLM recovery to actionable interactive failures

## Status

accepted, supersedes ADR-0021 (partial; see Supersedes)

## Date

2026-11-20

## Supersedes

ADR-0021 (partial: limit new error checkpoints and live retry prompts to retryable failures in explicitly interactive runs; replace visibility of non-retryable checkpoints through startup notices and actionable `/resume` lists). Atomic persistence, eligible retryable recovery, trace reuse, nonfatal save failures, ownership, and no automatic file cleanup remain in force.

## Context

ADR-0021 combines checkpoint persistence with live operator recovery. Headless runs cannot answer that prompt, and context/permanent checkpoints cannot usefully resume. Eligibility enforced only in Telegram can be bypassed by another controller caller.

## Considered Options

- Infer interactivity from callbacks: rejected because logging/notification callbacks do not imply usable operator controls.
- Remove all recovery: rejected because retryable continuation preserves completed tool results.
- Explicit per-run recovery capability and controller-owned eligibility: chosen to remove unusable recovery without removing useful continuation.

## Decision

Enable interactive LLM recovery explicitly per run, default false. Telegram root requests/resume enable it; headless runners do not. Check eligibility before saving a checkpoint or waiting. Non-retryable failures and headless terminal failures return fixed safe guidance after the existing automatic request policy, with cancellation taking precedence.

The controller/recovery layer owns resume eligibility, enforced before manual authorization/send. Telegram and startup consult it for filtered presentation. Retain older non-retryable files without mutation, but never advertise them as resumable.

Preserve ADR-0035 attempt-state accounting, no-refill/no-tool-replay, exact-snapshot send-gate retirement, pre-send snapshot preservation and cleanup. Trace reuse does not transfer turn/grant ownership; ADR-0033 fresh ownership remains unchanged. Tool-confirmation permissions are independent.

## Consequences

- Headless and non-retryable failures release execution without an unanswerable wait.
- Interactive retryable failures retain in-memory retry and optional durable recovery.
- Adapters cannot bypass execution eligibility, but must explicitly enable supported interactive recovery.
- Older non-retryable files occupy disk until operator cleanup; no migration or deletion infrastructure is added.
- Terminal failure propagation must remain truthful across callers without whole-task replay.

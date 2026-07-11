# Use Shell Guard for interactive shell tool decisions

## Status

Accepted

## Date

2026-07-08

## Supersedes

None

## Context

`smallpieclaw` already routes built-in shell execution through `BuiltinExecutor._exec_shell()`, but the current safety gate is a small regex list plus generic confirmation. Agents can overuse shell commands while trying to solve a task, and a generic prompt does not give users enough command-specific explanation or policy reuse.

The agent is intentionally used to control the host where it is installed, so the first version must not isolate the agent from the local system or replace the user's shell. The guard must also preserve existing headless/sub-agent dangerous-shell protections and the structured logging/redaction principles already accepted by ADR-0004.

## Decision

We will add Shell Guard as the architectural pattern for interactive depth-0 shell tool decisions. Shell Guard sits at the built-in shell preflight boundary, parses and classifies commands before execution, maps decisions to allow/ask/deny, records detailed metadata, and uses Telegram for active-mode ask decisions.

Shell Guard uses an Aegish-inspired validation pipeline with mature shell structure parsing, read-only LLM classification, local TOML policy rules, and classify-mode telemetry. It does not replace the shell, does not add OS/process isolation in v0.1, and does not execute commands merely to classify them.

## Consequences

- Positive: Shell decisions become explainable, auditable, and reusable through local policy instead of one-off regex prompts.
- Positive: Common known command shapes can be handled without repeated LLM calls once policy rules are created.
- Positive: The guard can collect classify-mode telemetry before active enforcement.
- Positive: The design preserves the existing shell backend and host-control use case.
- Negative: Shell execution gains a cross-cutting policy, logging, Telegram, and CLI surface that must be kept consistent.
- Negative: LLM classification can still be wrong; users must retain final control for ask decisions.
- Negative: Detailed metadata can contain sensitive command data, requiring recursive redaction and owner-only storage.
- Follow-up: Future changes may revisit OS-level sandboxing or stronger provenance checks, but those are separate decisions.

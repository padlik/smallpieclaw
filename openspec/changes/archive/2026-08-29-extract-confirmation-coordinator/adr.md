# ADR Review Manifest

- Status: completed
- Review date: 2026-08-28

## Review Summary

ADR review completed for this change. One durable architectural decision identified: the unification of all confirmation signaling flows into a single `ConfirmationManager` instance, replacing ADR-0011's `_prompt_approval_set` mechanism with a `_coordinator` reference.

## In-Force ADRs Reviewed

- ADR-0008: Use facade + handler package for built-in tools — preserved the confirmation seam for this extraction; explicitly aligned.
- ADR-0010: Zone-based file access control — untouched; zone classification and grants are outside the coordinator's scope.
- ADR-0011: Use per-prompt scope for operator approval grants — superseded by ADR-0024 (this change). Per-prompt TTL decision preserved; mechanism changed from `_prompt_approval_set` to `_coordinator`.
- ADR-0012: Use nsjail for shell isolation — the security review outcome that unblocked this extraction.

## New Durable ADRs Created

- `adr/0024-unify-confirmation-signaling-in-coordinator.md` — Unifies all four confirmation signaling flows (tool confirm d0, tool confirm d≥1, step extension, LLM error retry) into `ConfirmationManager`. Supersedes ADR-0011's `_prompt_approval_set` mechanism while preserving its per-prompt TTL, shared-set, and shell-never-auto-approved decisions. Records the intentional behavior change (orphaned sub-agent fail-closed: prompt → block).
# ADR Review Manifest

- Status: completed
- Review date: 2026-07-22

## Review Summary

ADR review completed for this change. All 11 in-force repository-level ADRs were reviewed.
None of the design decisions in this change introduce a major durable architectural commitment
(pattern, technology, boundary, or contract that affects future changes). The four decisions
in design.md — test file placement, docstring-only documentation, regression-test framing for
error paths, and dead code removal ordering — are tactical implementation choices scoped to
this change only.

## In-Force ADRs Reviewed

- ADR-0001: use-file-backed-provider-secrets
- ADR-0002: vault-secret-manager
- ADR-0003: use-toml-vault-format
- ADR-0004: structured-primary-agent-logging
- ADR-0005: use-sub-agent-supervisor-boundary
- ADR-0006: use-source-categories-for-agent-visibility
- ADR-0007: use-agent-runtime-for-construction
- ADR-0008: use-facade-handler-package-for-builtin-tools
- ADR-0009: native-tool-calling
- ADR-0010: zone-based-file-access-control
- ADR-0011: per-prompt-approval-scope

No ADR conflicts or supersession requirements identified.

## New Durable ADRs Created

- None — no major durable architectural decisions were introduced by this change.

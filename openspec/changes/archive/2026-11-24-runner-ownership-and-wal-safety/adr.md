# ADR Review Manifest

- Status: completed
- Review date: 2026-11-21

## Review Summary

ADR review completed for this change. The inventory contains 48 decision records (0001 through 0048) and nine glossary companions, 57 Markdown files total. Supersedes links were followed with partial supersessions applied only to their stated scope; the highest sequence is 0048.

No major durable architectural decisions were introduced and no new repository-level ADR files were created. This change repairs lifecycle enforcement within ADR-0005/0007/0033's existing boundaries and removes destructive graph recovery within the residual ADR-0039/0042 graph-only containment contract. No accepted decision requires application deletion of graph WAL files. The local Future bookkeeping and stopping handoff are tactical enforcement, not a new executor or admission policy.

## In-Force ADRs Reviewed

The complete reviewed in-force set, including surviving clauses of partially superseded records:

- ADR-0003, ADR-0004, ADR-0005, ADR-0006, ADR-0007, ADR-0008, ADR-0009.
- ADR-0012, ADR-0013, ADR-0014, ADR-0015, ADR-0016, ADR-0019.
- ADR-0020, ADR-0021, ADR-0022, ADR-0023, ADR-0025, ADR-0026, ADR-0027, ADR-0028, ADR-0029.
- ADR-0030, ADR-0031, ADR-0032, ADR-0033, ADR-0034, ADR-0035, ADR-0036, ADR-0037, ADR-0038, ADR-0039.
- ADR-0040, ADR-0041, ADR-0042, ADR-0043, ADR-0044, ADR-0045, ADR-0046, ADR-0047, ADR-0048.

Fully superseded records 0001, 0002, 0010, 0011, 0017, 0018 and 0024 were read as historical context, not as independent live constraints.

Relevant preserved commitments:

- `adr/0005-use-sub-agent-supervisor-boundary.md`: supervisor owns registration, submission, callback routing and cleanup.
- `adr/0006-use-source-categories-for-agent-visibility.md`: source categories, visibility and capacity-counting policy remain unchanged.
- `adr/0007-use-agent-runtime-for-construction.md`: construction remains separate from supervision/orchestration; later admission supersessions remain respected.
- `adr/0033-separate-turn-owned-and-scheduled-executions.md`: immutable origin/result ownership, cancellation scope and bounded draining remain unchanged.
- `adr/0039-pin-graph-models-independently-of-main-selection.md`: only its surviving ownership, independence, failure containment, dry-run and bounded-shutdown clauses apply.
- `adr/0042-configure-auxiliary-inference-independently-of-chat.md`: explicit independent graph settings, sanitized optional-component degradation, owned extraction clients and writer shutdown remain unchanged.

ADR-0025 concerns the separate structured-log SQLite store; this change neither modifies it nor applies graph WAL policy to it. No previous ADR is edited.

## New Durable ADRs Created

- None — no major durable architectural decisions were introduced.

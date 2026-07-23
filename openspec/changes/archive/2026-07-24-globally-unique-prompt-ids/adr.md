# ADR Review Manifest

- Status: completed
- Review date: 2026-07-23

## Review Summary

ADR review completed for this change. One major durable architectural decision was identified: the shift from monotonic integer prompt IDs to globally-unique ULID string IDs. This decision establishes a long-term contract (the prompt ID format and uniqueness guarantee) that will affect future changes involving prompt referencing, log correlation, and operator UX.

## In-Force ADRs Reviewed

- ADR-0004: Use structlog for structured-primary agent logging — `prompt_id` is an opaque observability field; the ULID format change is compatible (no supersession needed).
- ADR-0011: Use per-prompt scope for operator approval grants — `_current_prompt_id` is a per-prompt scope marker, not the approval-set key; the int→str change is compatible (no supersession needed).
- ADR-0005, ADR-0006, ADR-0007, ADR-0008, ADR-0009, ADR-0010, ADR-0012: reviewed; no interaction with prompt ID format.

## New Durable ADRs Created

- `adr/0013-use-ulid-for-globally-unique-prompt-ids.md` — records the decision to use ULID-format string IDs (26-char Crockford base32, inline-generated) as the globally-unique, time-sortable operator-facing prompt handle, replacing the monotonic integer counter.
# ADR Review Manifest

- Status: completed
- Review date: 2026-11-21

## Review Summary

ADR review completed for this change. All 48 numbered repository ADRs were read and their supersession links assessed; the highest existing sequence is 0048. Fully superseded records are historical only. Partially superseded records constrain this change only through their surviving decisions.

This change tightens the independently owned optional summary budget to zero retries. It retains ADR-0043's pinned-client, success-only publication, cancellation, and run-local failure policy and ADR-0035's agent-owned attempt accounting. The main logical request's configured retry allowance remains unchanged. No existing accepted ADR is edited or superseded.

## In-Force ADRs Reviewed

The following accepted records in repository `adr/` retain live decisions:

- ADR-0003, ADR-0004, ADR-0005, ADR-0006, ADR-0007, ADR-0008, ADR-0009.
- ADR-0012, ADR-0013, ADR-0014, ADR-0015, ADR-0016, ADR-0019.
- ADR-0020, ADR-0021, ADR-0022, ADR-0023, ADR-0025, ADR-0026, ADR-0027, ADR-0028, ADR-0029.
- ADR-0030, ADR-0031, ADR-0032, ADR-0033, ADR-0034, ADR-0035, ADR-0036, ADR-0037, ADR-0038, ADR-0039.
- ADR-0040, ADR-0041, ADR-0042, ADR-0043, ADR-0044, ADR-0045, ADR-0046, ADR-0047, ADR-0048.

Directly relevant: ADR-0043 (compaction and separate summary state), ADR-0035 (attempt accounting), ADR-0036/0041 (admitted immutable pins), ADR-0023 (lifecycle accounting), and ADR-0048 with surviving ADR-0021 (recovery/cancellation). Their ownership and lifecycle boundaries stay intact.

## New Durable ADRs Created

- None — no major durable architectural decisions were introduced and no new repository-level ADR files were created. This is a tactical tightening of one optional call's independent retry budget, captured in the delta specification and design.
